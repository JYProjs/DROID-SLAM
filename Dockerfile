FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04

# Set arguments and env variables
ARG DEBIAN_FRONTEND=noninteractive

ENV NVIDIA_VISIBLE_DIVICES all
ENV NVIDIA_DRIVER_CAPABILITIES graphics,utility,compute


# Fetch nvidia signing keys
RUN apt-key del 7fa2af80
RUN apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/3bf863cc.pub

# Install ubuntu base packages
RUN apt-get update && apt install -y --no-install-recommends \
    software-properties-common \
    apt-utils \
    dbus-x11 \
    libglvnd0 \
    libgl1 \
    libglx0 \
    libegl1 \
    libxext6 \
    libx11-6 \
    libgl1-mesa-dev \
    libglew-dev 


RUN apt-get update && apt install -y --no-install-recommends \
    build-essential \
    libboost-all-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    ninja-build

# Installing required utilities
RUN apt-get update && apt-get install -y \
    curl \
    git \
    ssh \
    unzip \
    vim \
    wget \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
RUN mkdir -p /root/
WORKDIR /root/

# Set user to root to avoid permission issues
USER root

# Install miniconda
RUN wget \
    https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh && \
    bash Miniconda3-latest-Linux-x86_64.sh -b && \
    rm -f Miniconda3-latest-Linux-x86_64.sh 

ENV PATH="/root/miniconda3/bin:${PATH}"

# Install Droid SLAM dependencies
# TODO: add minor version for python
RUN conda install -y python=3.9.19

RUN conda install -y pytorch==1.10.1 torchvision==0.11.2 torchaudio==0.10.1 cudatoolkit=11.3 -c pytorch -c conda-forge

RUN pip install gdown matplotlib==3.9.2 open3d==0.18.0 opencv-python==4.10.0.84 torch-scatter==2.1.2 tensorboard==2.17.1 scipy==1.13.1 tqdm==4.66.5 pyyaml==6.0.2 evo

# what does this do?
RUN conda install -y -c conda-forge suitesparse 

# Clone repository
WORKDIR /root
RUN git clone --recursive https://github.com/JYProjs/DROID-SLAM.git

# Install extensions
# pip install -e
RUN cd DROID-SLAM && \
    python setup.py install

# Cleanup
RUN rm -rf DROID_SLAM

# Set entry commands
ENTRYPOINT ["/bin/bash"]
