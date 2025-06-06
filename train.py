import sys
sys.path.append('droid_slam')

import cv2
import numpy as np
from collections import OrderedDict

import torch
import torch.optim as optim
from torch.utils.data import DataLoader
from data_readers.factory import dataset_factory

from lietorch import SO3, SE3, Sim3
from geom import losses
from geom.losses import geodesic_loss, residual_loss, flow_loss
from geom.graph_utils import build_frame_graph

# network
from droid_net import DroidNet
from logger import Logger

# DDP training
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import wandb



def setup_ddp(gpu, args):
    dist.init_process_group(                                   
    	backend='gloo',                                         
   		init_method='env://',     
    	world_size=args.world_size,                              
    	rank=gpu)

    torch.manual_seed(0)
    torch.cuda.set_device(gpu)

def show_image(image):
    image = image.permute(1, 2, 0).cpu().numpy()
    cv2.imshow('image', image / 255.0)
    cv2.waitKey()

def train(gpu, args):
    """ Test to make sure project transform correctly maps points """

    # coordinate multiple GPUs
    setup_ddp(gpu, args)
    rng = np.random.default_rng(12345)

    N = args.n_frames
    model = DroidNet()
    model.cuda()
    model.train()

    model = DDP(model, device_ids=[gpu], find_unused_parameters=False)

    # load pretrained weight
    # if args.ckpt is not None:
    #     ckpt = torch.load(args.ckpt)
    #     model.load_state_dict(ckpt['model_state_dict'])
    #     optimizer.load_state_dict(ckpt['optimizer_state_dict'])
    #     scheduler.load_state_dict(ckpt['scheduler_state_dict'])

    # load droid.pth
    if args.ckpt is not None:
        # load checkpoint and change shape of weights to match network shape
        state_dict = torch.load(args.ckpt)
        
        # change shape in state dict to match network
        state_dict["module.update.weight.2.weight"] = state_dict["module.update.weight.2.weight"][:2]
        state_dict["module.update.weight.2.bias"] = state_dict["module.update.weight.2.bias"][:2]
        state_dict["module.update.delta.2.weight"] = state_dict["module.update.delta.2.weight"][:2]
        state_dict["module.update.delta.2.bias"] = state_dict["module.update.delta.2.bias"][:2]

        model.load_state_dict(state_dict)

    # fetch dataloaders
    db = dataset_factory(['tartan'], datapath=args.datapath, n_frames=args.n_frames, fmin=args.fmin, fmax=args.fmax)
    val_db = dataset_factory(['tartan'], datapath=args.val_datapath, n_frames=args.n_frames, fmin=args.fmin, fmax=args.fmax)

    # create distributed sampler
    train_sampler = torch.utils.data.distributed.DistributedSampler(
        db, shuffle=True, num_replicas=args.world_size, rank=gpu)
    val_sampler = torch.utils.data.distributed.DistributedSampler(
        val_db, shuffle=False, num_replicas=args.world_size, rank=gpu)

    train_loader = DataLoader(db, batch_size=args.batch, sampler=train_sampler, num_workers=2)
    val_loader = DataLoader(val_db, batch_size=2, sampler=val_sampler, num_workers=2)


    # fetch optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, 
    args.lr, args.steps, pct_start=0.01, cycle_momentum=False)

    logger = Logger(args.name, scheduler)
    should_keep_training = True
    total_steps = 0
    # log_freq = 10

    # wandb.init
    if gpu == 0:
        run = wandb.init(project="droid_slam_laparoscope", config=args, save_code=True)

    # geo_loss_total = 0
    # flo_loss_total = 0
    # res_loss_total = 0
    # loss_total = 0

    while should_keep_training:
        for i_batch, item in enumerate(train_loader):
            optimizer.zero_grad()

            images, poses, disps, intrinsics = [x.to('cuda') for x in item]

            # convert poses w2c -> c2w
            Ps = SE3(poses).inv()
            Gs = SE3.IdentityLike(Ps)

            # randomize frame graph
            if np.random.rand() < 0.5:
                graph = build_frame_graph(poses, disps, intrinsics, num=args.edges)
            
            else:
                graph = OrderedDict()
                for i in range(N):
                    graph[i] = [j for j in range(N) if i!=j and abs(i-j) <= 2]
            
            # fix first to camera poses
            Gs.data[:,0] = Ps.data[:,0].clone()
            Gs.data[:,1:] = Ps.data[:,[1]].clone()
            disp0 = torch.ones_like(disps[:,:,3::8,3::8])

            # perform random restarts
            r = 0
            while r < args.restart_prob:
                r = rng.random()
                
                intrinsics0 = intrinsics / 8.0
                poses_est, disps_est, residuals = model(Gs, images, disp0, intrinsics0, 
                    graph, num_steps=args.iters, fixedp=2)

                geo_loss, geo_metrics = losses.geodesic_loss(Ps, poses_est, graph, do_scale=False)
                res_loss, res_metrics = losses.residual_loss(residuals)
                flo_loss, flo_metrics = losses.flow_loss(Ps, disps, poses_est, disps_est, intrinsics, graph)

                loss = args.w1 * geo_loss + args.w2 * res_loss + args.w3 * flo_loss
                loss.backward()

                Gs = poses_est[-1].detach()
                disp0 = disps_est[-1][:,:,3::8,3::8].detach()


            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
            optimizer.step()
            scheduler.step()
            
            ## TRAINING METRICS
            metrics = {}
            metrics.update(geo_metrics)
            metrics.update(res_metrics)
            metrics.update(flo_metrics)
            # Acquire quantitative metrics
            rot_error = metrics.get('rot_error')
            tr_error = metrics.get('tr_error')
            f_error = metrics.get('f_error')

            # validation loop
            if total_steps!=0 and total_steps % 5000 == 0:
                model.eval()
                # make numpy arrays for metrics
                val_rot_error = 0.0
                val_tr_error = 0.0
                val_f_error = 0.0
                with torch.no_grad():
                    for v_batch, val_item in enumerate(val_loader):
                        if v_batch == 500:
                            break
                        images, poses, disps, intrinsics = [x.to('cuda') for x in val_item]

                        # convert poses w2c -> c2w
                        Ps = SE3(poses).inv()
                        Gs = SE3.IdentityLike(Ps)

                        # fix first to camera poses
                        Gs.data[:,0] = Ps.data[:,0].clone()
                        Gs.data[:,1:] = Ps.data[:,[1]].clone()
                        disp0 = torch.ones_like(disps[:,:,3::8,3::8])

                        val_graph = OrderedDict()
                        for i in range(N):
                            val_graph[i] = [j for j in range(N) if i!=j and abs(i-j) <= 2]

                        poses_est, disps_est, residuals = model(Gs, images, disp0, intrinsics / 8.0,
                            graph=val_graph, num_steps=args.iters, fixedp=2)
                        val_geo_loss, val_geo_metrics = losses.geodesic_loss(Ps, poses_est, graph)
                        # val_res_loss, val_res_metrics = losses.residual_loss(residuals)
                        val_flo_loss, val_flo_metrics = losses.flow_loss(Ps, disps, poses_est, disps_est, intrinsics, val_graph)
                        val_rot_error += (val_geo_metrics.get('rot_error'))
                        val_tr_error += (val_geo_metrics.get('tr_error'))
                        val_f_error += (val_flo_metrics.get('f_error'))

                        # convert to tensors
                        cuda0 = torch.device('cuda:0')
                        tensor_val_rot_err = torch.tensor(val_rot_error, device=cuda0)
                        tensor_val_tr_err = torch.tensor(val_tr_error, device=cuda0)
                        tensor_val_f_err = torch.tensor(val_f_error, device=cuda0)

                    # val_rot_error = np.mean(val_rot_error)
                    # val_tr_error = np.mean(val_tr_error)
                    # val_f_error = np.mean(val_f_error)
                    dist.barrier()
                    dist.all_reduce(tensor_val_rot_err, dist.ReduceOp.SUM, async_op=False)
                    dist.all_reduce(tensor_val_tr_err, dist.ReduceOp.SUM, async_op=False)
                    dist.all_reduce(tensor_val_f_err, dist.ReduceOp.SUM, async_op=False)

                    val_rot_error /= ((v_batch) * args.world_size)
                    val_tr_error /= ((v_batch) * args.world_size)
                    val_f_error /= ((v_batch) * args.world_size)

                if gpu==0:
                    wandb.log(
                        {
                            # "val_geo_loss":val_geo_loss,
                            # "val_res_loss":val_res_loss,
                            # "val_flo_loss":val_flo_loss,
                            "val_rot_error":val_rot_error,
                            "val_tr_error":val_tr_error,
                            "val_f_error":val_f_error
                        },
                        step=total_steps)
                    
                model.train()
           
            total_steps += 1

            if gpu == 0:
                wandb.log(
                {
                    "geo_loss":geo_loss,
                    "res_loss":res_loss,
                    "flo_loss":flo_loss,
                    "loss":loss,
                    "lr":optimizer.param_groups[0]["lr"],
                    "rot_error":rot_error,
                    "tr_error":tr_error,
                    "flo_error":f_error
                },
                step=total_steps)
                logger.push(metrics)

            if total_steps % 10000 == 0 and gpu == 0:
                checkpoint = {
                    'params':args,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'scheduler_state_dict': scheduler.state_dict()
                }
                PATH = '/workspace/DROID_SLAM/trained_weights/new_train/04282025/%s_%s_%06d.pth' % (run.id, args.name, total_steps)
                torch.save(checkpoint, PATH)

            if total_steps >= args.steps:
                should_keep_training = False
                break
    wandb.finish(
        exit_code = 0,
        quiet = 0,
    )
    dist.destroy_process_group()
                

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='bla', help='name your experiment')
    parser.add_argument('--ckpt', help='checkpoint to restore')
    parser.add_argument('--datasets', nargs='+', help='lists of datasets for training')
    parser.add_argument('--datapath', default='datasets/TartanAir', help="path to training dataset directory")
    parser.add_argument('--val_datapath', help="path to validation dataset directory")
    parser.add_argument('--gpus', type=int, default=4)

    parser.add_argument('--batch', type=int, default=1)
    parser.add_argument('--iters', type=int, default=15)
    parser.add_argument('--steps', type=int, default=250000)
    parser.add_argument('--lr', type=float, default=2.5e-4)
    parser.add_argument('--clip', type=float, default=2.5)
    parser.add_argument('--n_frames', type=int, default=7)

    parser.add_argument('--w1', type=float, default=10.0)
    parser.add_argument('--w2', type=float, default=0.00)
    parser.add_argument('--w3', type=float, default=0.00)

    parser.add_argument('--fmin', type=float, default=8.0)
    parser.add_argument('--fmax', type=float, default=96.0)
    parser.add_argument('--noise', action='store_true')
    parser.add_argument('--scale', action='store_true')
    parser.add_argument('--edges', type=int, default=24)
    parser.add_argument('--restart_prob', type=float, default=0.2)

    args = parser.parse_args()

    args.world_size = args.gpus
    print(args)

    import os
    if not os.path.isdir('checkpoints'):
        os.mkdir('checkpoints')

    args = parser.parse_args()
    args.world_size = args.gpus

    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12396'
    mp.spawn(train, nprocs=args.gpus, args=(args,))
