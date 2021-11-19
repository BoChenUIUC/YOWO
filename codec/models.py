from __future__ import print_function
import os
import io
import sys
import time
import math
import random
import numpy as np
import subprocess as sp
import shlex
import cv2

import torch
import torch.nn as nn
import torch.utils.data
import torch.nn.functional as F
import torch.optim as optim
import torch.backends.cudnn as cudnn
from torch.autograd import Function
from torchvision import transforms
sys.path.append('..')
from compressai.layers import GDN,ResidualBlock,AttentionBlock
from compressai.models import CompressionModel
from codec.entropy_models import RecProbModel,JointAutoregressiveHierarchicalPriors,MeanScaleHyperPriors
from compressai.models.waseda import Cheng2020Attention
import pytorch_msssim
from datasets.clip import *
from core.utils import *

def get_codec_model(name):
    if name in ['MLVC','RLVC','DVC','RAW']:
        model_codec = LearnedVideoCodecs(name)
    elif name in ['DCVC','DCVC_v2']:
        model_codec = DCVC(name)
    elif name in ['SPVC']:
        model_codec = SPVC(name)
    elif name in ['SCVC']:
        model_codec = SCVC(name)
    elif name in ['AE3D']:
        model_codec = AE3D(name)
    elif name in ['x264','x265']:
        model_codec = StandardVideoCodecs(name)
    else:
        print('Cannot recognize codec:', name)
        exit(1)
    return model_codec

def compress_video(model, frame_idx, cache, startNewClip):
    if model.name in ['MLVC','RLVC','DVC','DCVC','DCVC_v2']:
        compress_video_sequential(model, frame_idx, cache, startNewClip)
    elif model.name in ['x265','x264']:
        compress_video_group(model, frame_idx, cache, startNewClip)
    elif model.name in ['SPVC','SCVC','AE3D']:
        compress_video_batch(model, frame_idx, cache, startNewClip)
            
def init_training_params(model):
    model.r_img, model.r_bpp, model.r_flow, model.r_aux = 1,1,1,1
    model.r_app, model.r_rec, model.r_warp, model.r_mc, model.r_ref_codec = 1,1,1,1,1
    
    model.r = 1024 # PSNR:[256,512,1024,2048] MSSSIM:[8,16,32,64]
    model.I_level = 27 # [37,32,27,22] poor->good quality
    
def update_training(model, epoch):
    # warmup with all gamma set to 1
    # optimize for bpp,img loss and focus only reconstruction loss
    # optimize bpp and app loss only
    
    # setup training weights
    if epoch <= 10:
        model.r_img, model.r_bpp, model.r_flow, model.r_aux = 1,1,1,1
        model.r_app, model.r_rec, model.r_warp, model.r_mc, model.r_ref_codec = 0,1,1,1,1
    else:
        model.r_img, model.r_bpp, model.r_flow, model.r_aux = 1,1,0,1
        model.r_app, model.r_rec, model.r_warp, model.r_mc, model.r_ref_codec = 0,1,0,0,0
    
    # whether to compute action detection
    doAD = True if model.r_app > 0 else False
    
    model.epoch = epoch
    
    return doAD
        
def compress_video_group(model, frame_idx, cache, startNewClip):
    if startNewClip:
        imgByteArr = io.BytesIO()
        width,height = shape
        fps = 25
        Q = 23#15,19,23,27
        GOP = 13
        output_filename = 'tmp/videostreams/output.mp4'
        if model.name == 'x265':
            cmd = f'/usr/bin/ffmpeg -y -s {width}x{height} -pixel_format bgr24 -f rawvideo -r {fps} -i pipe: -vcodec libx265 -pix_fmt yuv420p -preset veryfast -tune zerolatency -x265-params "crf={Q}:keyint={GOP}:verbose=1" {output_filename}'
        elif model.name == 'x264':
            cmd = f'/usr/bin/ffmpeg -y -s {width}x{height} -pixel_format bgr24 -f rawvideo -r {fps} -i pipe: -vcodec libx264 -pix_fmt yuv420p -preset veryfast -tune zerolatency -crf {Q} -g {GOP} -bf 2 -b_strategy 0 -sc_threshold 0 -loglevel debug {output_filename}'
        else:
            print('Codec not supported')
            exit(1)
        # bgr24, rgb24, rgb?
        #process = sp.Popen(shlex.split(f'/usr/bin/ffmpeg -y -s {width}x{height} -pixel_format bgr24 -f rawvideo -r {fps} -i pipe: -vcodec {libname} -pix_fmt yuv420p -crf 24 {output_filename}'), stdin=sp.PIPE)
        process = sp.Popen(shlex.split(cmd), stdin=sp.PIPE)
        raw_clip = cache['clip']
        for img in raw_clip:
            process.stdin.write(np.array(img).tobytes())
        # Close and flush stdin
        process.stdin.close()
        # Wait for sub-process to finish
        process.wait()
        # Terminate the sub-process
        process.terminate()
        # check video size
        video_size = os.path.getsize(output_filename)*8
        # Use OpenCV to read video
        clip = []
        cap = cv2.VideoCapture(output_filename)
        # Check if camera opened successfully
        if (cap.isOpened()== False):
            print("Error opening video stream or file")
        # Read until video is completed
        while(cap.isOpened()):
            # Capture frame-by-frame
            ret, img = cap.read()
            if ret != True:break
            clip.append(transforms.ToTensor()(img).cuda())
        # When everything done, release the video capture object
        cap.release()
        assert len(clip) == len(raw_clip), 'Clip size mismatch'
        # create cache
        cache['bpp_est'] = {}
        cache['img_loss'] = {}
        cache['bpp_act'] = {}
        cache['psnr'] = {}
        cache['msssim'] = {}
        cache['aux'] = {}
        bpp = video_size*1.0/len(clip)/(height*width)
        for i in range(frame_idx-1,len(clip)):
            Y1_raw = transforms.ToTensor()(raw_clip[i]).cuda()
            Y1_com = clip[i]
            cache['img_loss'][i] = torch.FloatTensor([0]).squeeze(0).cuda(0)
            cache['bpp_est'][i] = torch.FloatTensor([0]).cuda(0)
            cache['psnr'][i] = PSNR(Y1_raw, Y1_com)
            cache['msssim'][i] = MSSSIM(Y1_raw, Y1_com)
            cache['bpp_act'][i] = torch.FloatTensor([bpp])
            cache['aux'][i] = torch.FloatTensor([0]).cuda(0)
        cache['clip'] = clip
    cache['max_seen'] = frame_idx-1
    return True
        
# depending on training or testing
# the compression time should be recorded accordinglly
def compress_video_sequential(model, frame_idx, cache, startNewClip):
    # process the involving GOP
    # if process in order, some frames need later frames to compress
    if startNewClip:
        # create cache
        cache['bpp_est'] = {}
        cache['img_loss'] = {}
        cache['aux'] = {}
        cache['bpp_act'] = {}
        cache['psnr'] = {}
        cache['msssim'] = {}
        cache['hidden'] = None
        cache['max_proc'] = -1
        # the first frame to be compressed in a video
    assert frame_idx>=1, 'Frame index less than 1'
    if cache['max_proc'] >= frame_idx-1:
        cache['max_seen'] = frame_idx-1
    else:
        ranges, cache['max_seen'], cache['max_proc'] = index2GOP(frame_idx-1, len(cache['clip']))
        for _range in ranges:
            prev_j = -1
            for loc,j in enumerate(_range):
                progressive_compression(model, j, prev_j, cache, loc==1, loc>=2)
                prev_j = j
        
def compress_video_batch(model, frame_idx, cache, startNewClip):
    # process the involving GOP
    # how to deal with backward P frames?
    # if process in order, some frames need later frames to compress
    if startNewClip:
        # create cache
        cache['bpp_est'] = {}
        cache['img_loss'] = {}
        cache['aux'] = {}
        cache['bpp_act'] = {}
        cache['msssim'] = {}
        cache['psnr'] = {}
        cache['end_of_batch'] = {}
        # frame shape
        _,h,w = cache['clip'][0].shape
        cache['hidden'] = model.init_hidden(h,w)
        cache['max_proc'] = -1
    if cache['max_proc'] >= frame_idx-1:
        cache['max_seen'] = frame_idx-1
    else:
        _range, cache['max_seen'], cache['max_proc'] = index2range(frame_idx-1, len(cache['clip']), startNewClip)
        parallel_compression(model, _range, cache)
        
def index2range(i, clip_len, startNewClip):
    GOP = 13
    bs = 4
    pos = i%GOP
    if pos == 0 or startNewClip:
        # compress as I frame
        return i,i,i
    else:
        # minimum of end of clip, end of batch, end of GOP
        end = min(clip_len-1,i//GOP*GOP+((pos-1)//bs+1)*bs,(i//GOP+1)*GOP-1)
        return range(i,end+1),i,end
      
def progressive_compression(model, i, prev, cache, P_flag, RPM_flag):
    # frame shape
    _,h,w = cache['clip'][0].shape
    # frames to be processed
    Y0_com = cache['clip'][prev].unsqueeze(0) if prev>=0 else None
    Y1_raw = cache['clip'][i].unsqueeze(0)
    # hidden variables
    if P_flag:
        hidden = model.init_hidden(h,w)
    else:
        hidden = cache['hidden']
    Y1_com,hidden,bpp_est,img_loss,aux_loss,bpp_act,psnr,msssim = model(Y0_com, Y1_raw, hidden, RPM_flag)
    cache['hidden'] = hidden
    cache['clip'][i] = Y1_com.detach().squeeze(0)
    cache['img_loss'][i] = img_loss
    cache['aux'][i] = aux_loss
    cache['bpp_est'][i] = bpp_est
    cache['psnr'][i] = psnr
    cache['msssim'][i] = msssim
    cache['bpp_act'][i] = bpp_act.cpu()
    #print(i,float(bpp_est),float(bpp_act),float(psnr))
    # we can record PSNR wrt the distance to I-frame to show error propagation)
        
def parallel_compression(model, _range, cache):
    # we can summarize the result for each index to study error propagation
    # I compression
    if not isinstance(_range,range):
        # I frame compression
        x_hat, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim = I_compression(cache['clip'][_range].unsqueeze(0), model.I_level)
        cache['clip'][_range] = x_hat.squeeze(0)
        cache['img_loss'][_range] = img_loss
        cache['aux'][_range] = aux_loss
        cache['bpp_est'][_range] = bpp_est
        cache['psnr'][_range] = psnr
        cache['msssim'][_range] = msssim
        cache['bpp_act'][_range] = bpp_act
        cache['end_of_batch'][_range] = True
        return
    # P compression
    img_list = [cache['clip'][_range[0]-1]]; idx_list = []
    for i in _range:
        img_list.append(cache['clip'][i])
        idx_list.append(i)
    x = torch.stack(img_list, dim=0)
    n = len(idx_list)
    x_hat, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim = model(x)
    for pos,j in enumerate(idx_list):
        cache['clip'][j] = x_hat[pos].squeeze(0).detach()
        cache['img_loss'][j] = img_loss
        cache['aux'][j] = aux_loss/n
        cache['bpp_est'][j] = bpp_est
        cache['psnr'][j] = psnr[pos]
        cache['msssim'][j] = msssim[pos]
        cache['bpp_act'][j] = bpp_act.cpu()
        cache['end_of_batch'][j] = True if pos == n-1 else False
            
def index2GOP(i, clip_len, fP = 6, bP = 6):
    # input: 
    # - idx: the frame index of interest
    # output: 
    # - ranges: the range(s) of GOP involving this frame
    # - max_seen: max index has been seen
    # - max_proc: max processed index
    # normally progressive coding will get 1 or 2 range(s)
    # parallel coding will get 1 range
    
    GOP = fP + bP + 1
    # 0 1  2  3  4  5  6  7  8  9  10 11 12 13
    # I fP fP fP fP fP fP bP bP bP bP bP bP I 
    ranges = []
    # <      case 1    >  
    # first time calling this function will mostly fall in case 1
    # case 1 will create one range
    if i%GOP <= fP:
        # e.g.: i=4,left=0,right=6,mid=0
        mid = i
        left = i
        right = min(i//GOP*GOP+fP,clip_len-1)
        _range = [j for j in range(mid,right+1)]
        ranges += [_range]
    #                     <      case 2   >
    # later calling this function will fall in case 2
    # case 2 will create one range if parallel or two ranges if progressive
    else:
        # e.g.: i=8,left=7,right=19,mid=13
        mid = min((i//GOP+1)*GOP,clip_len-1)
        left = i
        right = min((i//GOP+1)*GOP+fP,clip_len-1)
        possible_I = (i//GOP+1)*GOP
        # first backward
        _range = [j for j in range(mid,left-1,-1)]
        ranges += [_range]
        # then forward
        if right >= mid+1:
            _range = [j for j in range(mid+1,right+1)]
            ranges += [_range]
    max_seen, max_proc = i, right
    return ranges, max_seen, max_proc
    
# DVC,RLVC,MLVC
# Need to measure time and implement decompression for demo
# cache should store start/end-of-GOP information for the action detector to stop; test will be based on it
class LearnedVideoCodecs(nn.Module):
    def __init__(self, name, channels=128, noMeasure=True):
        super(LearnedVideoCodecs, self).__init__()
        self.name = name 
        device = torch.device('cuda')
        self.optical_flow = OpticalFlowNet()
        self.MC_network = MCNet()
        if name in ['MLVC','RLVC','DVC']:
            self.image_coder_name = 'bpg' 
        elif 'RAW' in name:
            self.image_coder_name = 'raw'
        else:
            print('I frame compression not implemented:',name)
            exit(1)
        if self.image_coder_name == 'deepcod':
            self._image_coder = DeepCOD()
        else:
            self._image_coder = None
        self.mv_codec = Coder2D(self.name, in_channels=2, channels=channels, kernel=3, padding=1, noMeasure=noMeasure)
        self.res_codec = Coder2D(self.name, in_channels=3, channels=channels, kernel=5, padding=2, noMeasure=noMeasure)
        self.channels = channels
        init_training_params(self)
        self.epoch = -1
        self.noMeasure = noMeasure
        
        # split on multi-gpus
        self.split()

    def split(self):
        if self._image_coder is not None:
            self._image_coder.cuda(0)
        self.optical_flow.cuda(0)
        self.mv_codec.cuda(0)
        self.MC_network.cuda(1)
        self.res_codec.cuda(1)

    def forward(self, Y0_com, Y1_raw, hidden_states, RPM_flag, use_psnr=True):
        # Y0_com: compressed previous frame, [1,c,h,w]
        # Y1_raw: uncompressed current frame
        if not self.noMeasure:
            self.enc_t = [];self.dec_t = []
        batch_size, _, Height, Width = Y1_raw.shape
        if self.name == 'RAW':
            bpp_est = bpp_act = metrics = torch.FloatTensor([0]).cuda(0)
            aux_loss = img_loss = torch.FloatTensor([0]).squeeze(0).cuda(0)
            return Y1_raw, hidden_states, bpp_est, img_loss, aux_loss, bpp_act, metrics
        if Y0_com is None:
            Y1_com, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim = I_compression(Y1_raw, self.I_level)
            return Y1_com, hidden_states, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
        # otherwise, it's P frame
        # hidden states
        rae_mv_hidden, rae_res_hidden, rpm_mv_hidden, rpm_res_hidden = hidden_states
        # estimate optical flow
        t_0 = time.perf_counter()
        mv_tensor, l0, l1, l2, l3, l4 = self.optical_flow(Y0_com, Y1_raw)
        if not self.noMeasure:
            self.enc_t += [time.perf_counter() - t_0]
        # compress optical flow
        mv_hat,rae_mv_hidden,rpm_mv_hidden,mv_act,mv_est,mv_aux = self.mv_codec(mv_tensor, rae_mv_hidden, rpm_mv_hidden, RPM_flag)
        if not self.noMeasure:
            self.enc_t += [self.mv_codec.enc_t]
            self.dec_t += [self.mv_codec.dec_t]
        # motion compensation
        t_0 = time.perf_counter()
        loc = get_grid_locations(batch_size, Height, Width).type(Y0_com.type())
        Y1_warp = F.grid_sample(Y0_com, loc + mv_hat.permute(0,2,3,1), align_corners=True)
        MC_input = torch.cat((mv_hat, Y0_com, Y1_warp), axis=1)
        Y1_MC = self.MC_network(MC_input.cuda(1))
        t_comp = time.perf_counter() - t_0
        if not self.noMeasure:
            self.enc_t += [t_comp]
            self.dec_t += [t_comp]
        warp_loss = calc_loss(Y1_raw, Y1_warp.to(Y1_raw.device), self.r, use_psnr)
        mc_loss = calc_loss(Y1_raw, Y1_MC.to(Y1_raw.device), self.r, use_psnr)
        # compress residual
        res_tensor = Y1_raw.cuda(1) - Y1_MC
        res_hat,rae_res_hidden,rpm_res_hidden,res_act,res_est,res_aux = self.res_codec(res_tensor, rae_res_hidden, rpm_res_hidden, RPM_flag)
        if not self.noMeasure:
            self.enc_t += [self.res_codec.enc_t]
            self.dec_t += [self.res_codec.dec_t]
        # reconstruction
        t_0 = time.perf_counter()
        Y1_com = torch.clip(res_hat + Y1_MC, min=0, max=1)
        if not self.noMeasure:
            self.dec_t += [time.perf_counter() - t_0]
        ##### compute bits
        # estimated bits
        bpp_est = (mv_est + res_est.cuda(0))/(Height * Width * batch_size)
        # actual bits
        bpp_act = (mv_act + res_act.to(mv_act.device))/(Height * Width * batch_size)
        # auxilary loss
        aux_loss = (mv_aux + res_aux.to(mv_aux.device))/2
        # calculate metrics/loss
        psnr = PSNR(Y1_raw, Y1_com.to(Y1_raw.device))
        msssim = MSSSIM(Y1_raw, Y1_com.to(Y1_raw.device))
        rec_loss = calc_loss(Y1_raw, Y1_com.to(Y1_raw.device), self.r, use_psnr)
        img_loss = (self.r_rec*rec_loss + self.r_warp*warp_loss + self.r_mc*mc_loss)
        img_loss += (l0+l1+l2+l3+l4)/5*1024*self.r_flow
        # hidden states
        hidden_states = (rae_mv_hidden.detach(), rae_res_hidden.detach(), rpm_mv_hidden, rpm_res_hidden)
        if not self.noMeasure:
            print(np.sum(self.enc_t),np.sum(self.dec_t),self.enc_t,self.dec_t)
        return Y1_com.cuda(0), hidden_states, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
        
    def loss(self, pix_loss, bpp_loss, aux_loss, app_loss=None):
        loss = self.r_img*pix_loss + self.r_bpp*bpp_loss + self.r_aux*aux_loss
        if self.name in ['MLVC','RAW']:
            if app_loss is not None:
                loss += self.r_app*app_loss
        return loss
    
    def init_hidden(self, h, w):
        rae_mv_hidden = torch.zeros(1,self.channels*4,h//4,w//4).cuda()
        rae_res_hidden = torch.zeros(1,self.channels*4,h//4,w//4).cuda()
        rpm_mv_hidden = torch.zeros(1,self.channels*2,h//16,w//16).cuda()
        rpm_res_hidden = torch.zeros(1,self.channels*2,h//16,w//16).cuda()
        return (rae_mv_hidden, rae_res_hidden, rpm_mv_hidden, rpm_res_hidden)
            
# DCVC?
# adding MC network doesnt help much
class DCVC(nn.Module):
    def __init__(self, name, channels=64, channels2=96, noMeasure=True):
        super(DCVC, self).__init__()
        device = torch.device('cuda')
        self.ctx_encoder = nn.Sequential(nn.Conv2d(channels+3, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels2, kernel_size=5, stride=2, padding=2)
                                        )
        self.ctx_decoder1 = nn.Sequential(nn.ConvTranspose2d(channels2, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        GDN(channels, inverse=True),
                                        nn.ConvTranspose2d(channels, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        GDN(channels, inverse=True),
                                        ResidualBlock(channels,channels),
                                        nn.ConvTranspose2d(channels, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        GDN(channels, inverse=True),
                                        ResidualBlock(channels,channels),
                                        nn.ConvTranspose2d(channels, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        )
        self.ctx_decoder2 = nn.Sequential(nn.Conv2d(channels*2, channels, kernel_size=3, stride=1, padding=1),
                                        ResidualBlock(channels,channels),
                                        ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, 3, kernel_size=3, stride=1, padding=1)
                                        )
        if name == 'DCVC_v2':
            self.MC_network = MCNet()
        self.feature_extract = nn.Sequential(nn.Conv2d(3, channels, kernel_size=3, stride=1, padding=1),
                                        ResidualBlock(channels,channels)
                                        )
        self.ctx_refine = nn.Sequential(ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, channels, kernel_size=3, stride=1, padding=1)
                                        )
        self.tmp_prior_encoder = nn.Sequential(nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels2, kernel_size=5, stride=2, padding=2)
                                        )
        self.optical_flow = OpticalFlowNet()
        self.mv_codec = Coder2D(name, in_channels=2, channels=channels, kernel=3, padding=1, noMeasure=noMeasure)
        self.entropy_bottleneck = JointAutoregressiveHierarchicalPriors(channels2)
        init_training_params(self)
        self.name = name
        self.channels = channels
        self.split()
        self.updated = False
        self.noMeasure = noMeasure

    def split(self):
        self.optical_flow.cuda(0)
        self.mv_codec.cuda(0)
        if self.name == 'DCVC_v2':
            self.MC_network.cuda(1)
        self.feature_extract.cuda(1)
        self.ctx_refine.cuda(1)
        self.tmp_prior_encoder.cuda(1)
        self.ctx_encoder.cuda(1)
        self.entropy_bottleneck.cuda(1)
        self.ctx_decoder1.cuda(1)
        self.ctx_decoder2.cuda(1)
    
    def forward(self, x_hat_prev, x, hidden_states, RPM_flag, use_psnr=True):
        if not self.updated and not self.training:
            self.entropy_bottleneck.update(force=True)
            
        if not self.noMeasure:
            self.enc_t = [];self.dec_t = []
            
        # I-frame compression
        if x_hat_prev is None:
            x_hat, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim = I_compression(x,self.I_level)
            return x_hat, hidden_states, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
        # size
        bs,c,h,w = x.size()
        
        # hidden states
        rae_mv_hidden, rpm_mv_hidden = hidden_states
                
        # motion estimation
        t_0 = time.perf_counter()
        mv, l0, l1, l2, l3, l4 = self.optical_flow(x, x_hat_prev)
        t_flow = time.perf_counter() - t_0
        if not self.noMeasure:
            self.enc_t += [t_flow]
        
        # compress optical flow
        mv_hat,rae_mv_hidden,rpm_mv_hidden,mv_act,mv_est,mv_aux = self.mv_codec(mv, rae_mv_hidden, rpm_mv_hidden, RPM_flag)
        if not self.noMeasure:
            self.enc_t += [self.mv_codec.enc_t]
            self.dec_t += [self.mv_codec.dec_t]
        
        # warping
        t_0 = time.perf_counter()
        loc = get_grid_locations(bs, h, w).cuda(1)
        if self.name == 'DCVC':
            # feature extraction
            x_feat = self.feature_extract(x_hat_prev.cuda(1))
            
            # motion compensation
            x_feat_warp = F.grid_sample(x_feat, loc + mv_hat.permute(0,2,3,1).cuda(1), align_corners=True) # the difference
            x_tilde = F.grid_sample(x_hat_prev.cuda(1), loc + mv_hat.permute(0,2,3,1).cuda(1), align_corners=True)
            warp_loss = calc_loss(x, x_tilde.to(x.device), self.r, use_psnr)
        else:
            # motion compensation
            x_warp = F.grid_sample(x_hat_prev.cuda(1), loc + mv_hat.permute(0,2,3,1).cuda(1), align_corners=True) # the difference
            warp_loss = calc_loss(x, x_warp.to(x.device), self.r, use_psnr)
            x_mc = self.MC_network(torch.cat((mv_hat.cuda(1), x_hat_prev.cuda(1), x_warp), axis=1).cuda(1))
            mc_loss = calc_loss(x, x_mc.to(x.device), self.r, use_psnr)
            
            # feature extraction
            x_feat_warp = self.feature_extract(x_mc)
        t_warp = time.perf_counter() - t_0
        if not self.noMeasure:
            self.enc_t += [t_warp]
            self.dec_t += [t_warp]
        
        # context refinement
        t_0 = time.perf_counter()
        context = self.ctx_refine(x_feat_warp)
        t_refine = time.perf_counter() - t_0
        if not self.noMeasure:
            self.enc_t += [t_refine]
            self.dec_t += [t_refine]
        
        # temporal prior
        t_0 = time.perf_counter()
        prior = self.tmp_prior_encoder(context)
        t_prior = time.perf_counter() - t_0
        if not self.noMeasure:
            self.enc_t += [t_prior]
            self.dec_t += [t_prior]
        
        # contextual encoder
        y = self.ctx_encoder(torch.cat((x, context.to(x.device)), axis=1).cuda(1))
        
        # entropy model
        if not self.noMeasure:
            y_string,shape = self.entropy_bottleneck.compress_slow(y, prior)
            y_hat = self.entropy_bottleneck.decompress_slow(y_string, shape, prior)
            y_est = torch.FloatTensor([0]).squeeze(0).to(x.device)
            y_act = self.entropy_bottleneck.get_actual_bits(y_string)
            self.enc_t += [self.entropy_bottleneck.enc_t]
            self.dec_t += [self.entropy_bottleneck.dec_t]
        else:
            y_hat, likelihoods = self.entropy_bottleneck(y, prior, training=self.training)
            y_est = self.entropy_bottleneck.get_estimate_bits(likelihoods)
            if not self.training:
                y_string = self.entropy_bottleneck.compress(y)
                y_act = self.entropy_bottleneck.get_actual_bits(y_string)
            y_act = y_est
        y_aux = self.entropy_bottleneck.loss()/self.channels
        
        # contextual decoder
        t_0 = time.perf_counter()
        x_hat = self.ctx_decoder1(y_hat)
        x_hat = self.ctx_decoder2(torch.cat((x_hat, context.to(x_hat.device)), axis=1)).to(x.device)
        if not self.noMeasure:
            self.dec_t += [time.perf_counter() - t_0]
        
        # estimated bits
        bpp_est = (mv_est + y_est.cuda(0))/(h * w * bs)
        # actual bits
        bpp_act = (mv_act + y_act.cuda(0))/(h * w * bs)
        #print(float(mv_est/(h * w * bs)), float(mv_act/(h * w * bs)), float(y_est/(h * w * bs)), float(y_act/(h * w * bs)))
        # auxilary loss
        aux_loss = (mv_aux + y_aux.cuda(0))/2
        # calculate metrics/loss
        psnr = PSNR(x, x_hat.cuda(0))
        msssim = MSSSIM(x, x_hat.cuda(0))
        rec_loss = calc_loss(x, x_hat.cuda(0), self.r, use_psnr)
        if self.name == 'DCVC':
            img_loss = (self.r_rec*rec_loss + self.r_warp*warp_loss)
        else:
            img_loss = (self.r_rec*rec_loss + self.r_warp*warp_loss + self.r_mc*mc_loss)
        img_loss += (l0+l1+l2+l3+l4)/5*1024*self.r_flow
        # hidden states
        hidden_states = (rae_mv_hidden.detach(), rpm_mv_hidden)
        if not self.noMeasure:
            print(np.sum(self.enc_t),np.sum(self.dec_t),self.enc_t,self.dec_t)
        return x_hat.cuda(0), hidden_states, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
        
    def loss(self, pix_loss, bpp_loss, aux_loss, app_loss=None):
        if app_loss is None:
            return self.r_img*pix_loss + self.r_bpp*bpp_loss + self.r_aux*aux_loss
        else:
            return self.r_app*app_loss + self.r_img*pix_loss + self.r_bpp*bpp_loss + self.r_aux*aux_loss
        
    def init_hidden(self, h, w):
        rae_mv_hidden = torch.zeros(1,self.channels*4,h//4,w//4).cuda()
        rpm_mv_hidden = torch.zeros(1,self.channels*2,h//16,w//16).cuda()
        return (rae_mv_hidden, rpm_mv_hidden)
        
class StandardVideoCodecs(nn.Module):
    def __init__(self, name):
        super(StandardVideoCodecs, self).__init__()
        self.name = name # x264, x265?
        self.placeholder = torch.nn.Parameter(torch.zeros(1))
    
    def loss(self, pix_loss, bpp_loss, aux_loss, app_loss=None):
        if app_loss is None:
            return self.r_img*pix_loss + self.r_bpp*bpp_loss + self.r_aux*aux_loss
        else:
            return self.r_app*app_loss + self.r_img*pix_loss + self.r_bpp*bpp_loss + self.r_aux*aux_loss
        
def I_compression(Y1_raw, I_level):
    # we can compress with bpg,deepcod ...
    batch_size, _, Height, Width = Y1_raw.shape
    prename = "tmp/frames/prebpg"
    binname = "tmp/frames/bpg"
    postname = "tmp/frames/postbpg"
    raw_img = transforms.ToPILImage()(Y1_raw.squeeze(0))
    raw_img.save(prename + '.jpg')
    pre_bits = os.path.getsize(prename + '.jpg')*8
    os.system('bpgenc -f 444 -m 9 ' + prename + '.jpg -o ' + binname + '.bin -q ' + str(I_level))
    os.system('bpgdec ' + binname + '.bin -o ' + postname + '.jpg')
    post_bits = os.path.getsize(binname + '.bin')*8/(Height * Width * batch_size)
    bpp_act = torch.FloatTensor([post_bits]).squeeze(0)
    bpg_img = Image.open(postname + '.jpg').convert('RGB')
    Y1_com = transforms.ToTensor()(bpg_img).cuda().unsqueeze(0)
    psnr = PSNR(Y1_raw, Y1_com)
    msssim = MSSSIM(Y1_raw, Y1_com)
    bpp_est = loss = aux_loss = torch.FloatTensor([0]).squeeze(0).cuda(0)
    return Y1_com, bpp_est, loss, aux_loss, bpp_act, psnr, msssim
    
def load_state_dict_only(model, state_dict, keyword):
    own_state = model.state_dict()
    for name, param in state_dict.items():
        if keyword not in name: continue
        if name in own_state:
            own_state[name].copy_(param)
    
def load_state_dict_whatever(model, state_dict):
    own_state = model.state_dict()
    for name, param in state_dict.items():
        if name.endswith("._offset") or name.endswith("._quantized_cdf") or name.endswith("._cdf_length") or name.endswith(".scale_table"):
             continue
        if name in own_state and own_state[name].size() == param.size():
            own_state[name].copy_(param)
            
def load_state_dict_all(model, state_dict):
    own_state = model.state_dict()
    for name, param in state_dict.items():
        if name.endswith("._offset") or name.endswith("._quantized_cdf") or name.endswith("._cdf_length") or name.endswith(".scale_table"):
             continue
        own_state[name].copy_(param)
    
def PSNR(Y1_raw, Y1_com, use_list=False):
    Y1_com = Y1_com.to(Y1_raw.device)
    log10 = torch.log(torch.FloatTensor([10])).squeeze(0).to(Y1_raw.device)
    if not use_list:
        train_mse = torch.mean(torch.pow(Y1_raw - Y1_com, 2))
        quality = 10.0*torch.log(1/train_mse)/log10
    else:
        b = Y1_raw.size()[0]
        quality = []
        for i in range(b):
            train_mse = torch.mean(torch.pow(Y1_raw[i].unsqueeze(0) - Y1_com[i].unsqueeze(0), 2))
            psnr = 10.0*torch.log(1/train_mse)/log10
            quality.append(psnr)
    return quality

def MSSSIM(Y1_raw, Y1_com, use_list=False):
    Y1_com = Y1_com.to(Y1_raw.device)
    if not use_list:
        quality = pytorch_msssim.ms_ssim(Y1_raw, Y1_com)
    else:
        b = Y1_raw.size()[0]
        quality = []
        for i in range(b):
            quality.append(pytorch_msssim.ms_ssim(Y1_raw[i].unsqueeze(0), Y1_com[i].unsqueeze(0)))
    return quality
    
def calc_loss(Y1_raw, Y1_com, r, use_psnr):
    if use_psnr:
        loss = torch.mean(torch.pow(Y1_raw - Y1_com.to(Y1_raw.device), 2))*r
    else:
        metrics = MSSSIM(Y1_raw, Y1_com.to(Y1_raw.device))
        loss = r*(1-metrics)
    return loss

# pyramid flow estimation
class OpticalFlowNet(nn.Module):
    def __init__(self):
        super(OpticalFlowNet, self).__init__()
        self.pool = nn.AvgPool2d(kernel_size=(2,2), stride=(2,2), padding=0)
        self.loss = LossNet()

    def forward(self, im1_4, im2_4):
        # im1_4,im2_4:[1,c,h,w]
        # flow_4:[1,2,h,w]
        batch, _, h, w = im1_4.size()
        
        im1_3 = self.pool(im1_4)
        im1_2 = self.pool(im1_3)
        im1_1 = self.pool(im1_2)
        im1_0 = self.pool(im1_1)

        im2_3 = self.pool(im2_4)
        im2_2 = self.pool(im2_3)
        im2_1 = self.pool(im2_2)
        im2_0 = self.pool(im2_1)

        flow_zero = torch.zeros(batch, 2, h//16, w//16).to(im1_4.device)

        loss_0, flow_0 = self.loss(flow_zero, im1_0, im2_0, upsample=False)
        loss_1, flow_1 = self.loss(flow_0, im1_1, im2_1, upsample=True)
        loss_2, flow_2 = self.loss(flow_1, im1_2, im2_2, upsample=True)
        loss_3, flow_3 = self.loss(flow_2, im1_3, im2_3, upsample=True)
        loss_4, flow_4 = self.loss(flow_3, im1_4, im2_4, upsample=True)

        return flow_4, loss_0, loss_1, loss_2, loss_3, loss_4

class LossNet(nn.Module):
    def __init__(self):
        super(LossNet, self).__init__()
        self.convnet = FlowCNN()
        self.upsample = nn.Upsample(scale_factor=2, mode='nearest')

    def forward(self, flow, im1, im2, upsample=True):
        if upsample:
            flow = self.upsample(flow)
        batch_size, _, H, W = flow.shape
        loc = get_grid_locations(batch_size, H, W).to(im1.device)
        flow = flow.to(im1.device)
        im1_warped = F.grid_sample(im1, loc + flow.permute(0,2,3,1), align_corners=True)
        res = self.convnet(im1_warped, im2, flow)
        flow_fine = res + flow # N,2,H,W

        im1_warped_fine = F.grid_sample(im1, loc + flow_fine.permute(0,2,3,1), align_corners=True)
        loss_layer = torch.mean(torch.pow(im1_warped_fine-im2,2))

        return loss_layer, flow_fine

class FlowCNN(nn.Module):
    def __init__(self):
        super(FlowCNN, self).__init__()
        self.conv1 = nn.Conv2d(8, 32, kernel_size=7, stride=1, padding=3)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=7, stride=1, padding=3)
        self.conv3 = nn.Conv2d(64, 32, kernel_size=7, stride=1, padding=3)
        self.conv4 = nn.Conv2d(32, 16, kernel_size=7, stride=1, padding=3)
        self.conv5 = nn.Conv2d(16, 2, kernel_size=7, stride=1, padding=3)

    def forward(self, im1_warp, im2, flow):
        x = torch.cat((im1_warp, im2, flow),axis=1)
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.conv4(x))
        x = self.conv5(x)
        return x

class ConvLSTM(nn.Module):
    def __init__(self, channels=128, forget_bias=1.0, activation=F.relu):
        super(ConvLSTM, self).__init__()
        self.conv = nn.Conv2d(2*channels, 4*channels, kernel_size=3, stride=1, padding=1)
        self._forget_bias = forget_bias
        self._activation = activation
        self._channels = channels

    def forward(self, x, state):
        c, h = torch.split(state,self._channels,dim=1)
        x = torch.cat((x, h), dim=1)
        y = self.conv(x)
        j, i, f, o = torch.split(y, self._channels, dim=1)
        f = torch.sigmoid(f + self._forget_bias)
        i = torch.sigmoid(i)
        c = c * f + i * self._activation(j)
        o = torch.sigmoid(o)
        h = o * self._activation(c)

        return h, torch.cat((c, h),dim=1)
    
def get_actual_bits(self, string):
    bits_act = torch.FloatTensor([len(b''.join(string))*8]).squeeze(0)
    return bits_act
        
def get_estimate_bits(self, likelihoods):
    log2 = torch.log(torch.FloatTensor([2])).squeeze(0).to(likelihoods.device)
    bits_est = torch.sum(torch.log(likelihoods)) / (-log2)
    return bits_est

class Coder2D(nn.Module):
    def __init__(self, keyword, in_channels=2, channels=128, kernel=3, padding=1, noMeasure=True):
        super(Coder2D, self).__init__()
        self.enc_conv1 = nn.Conv2d(in_channels, channels, kernel_size=kernel, stride=2, padding=padding)
        self.enc_conv2 = nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding)
        self.enc_conv3 = nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding)
        self.enc_conv4 = nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, bias=False)
        self.gdn1 = GDN(channels)
        self.gdn2 = GDN(channels)
        self.gdn3 = GDN(channels)
        self.dec_conv1 = nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1)
        self.dec_conv2 = nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1)
        self.dec_conv3 = nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1)
        self.dec_conv4 = nn.ConvTranspose2d(channels, in_channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1)
        self.igdn1 = GDN(channels, inverse=True)
        self.igdn2 = GDN(channels, inverse=True)
        self.igdn3 = GDN(channels, inverse=True)
        if keyword in ['MLVC','RLVC']:
            # for recurrent sequential model
            self.entropy_bottleneck = RecProbModel(channels)
            self.conv_type = 'rec'
            self.entropy_type = 'rpm'
        elif keyword in ['attn']:
            # for batch model
            self.entropy_bottleneck = MeanScaleHyperPriors(channels,useAttention=True)
            self.conv_type = 'attn'
            self.entropy_type = 'mshp'
        elif keyword in ['mshp']:
            # for image codec, single frame
            self.entropy_bottleneck = MeanScaleHyperPriors(channels,useAttention=False)
            self.conv_type = 'non-rec' # not need for single image compression
            self.entropy_type = 'mshp'
        elif keyword in ['DVC','base','DCVC','DCVC_v2']:
            # for sequential model with no recurrent network
            from compressai.entropy_models import EntropyBottleneck
            EntropyBottleneck.get_actual_bits = get_actual_bits
            EntropyBottleneck.get_estimate_bits = get_estimate_bits
            self.entropy_bottleneck = EntropyBottleneck(channels)
            self.conv_type = 'non-rec'
            self.entropy_type = 'base'
        else:
            print('Bottleneck not implemented for:',keyword)
            exit(1)
        print('Conv type:',self.conv_type,'entropy type:',self.entropy_type)
        self.channels = channels
        if self.conv_type == 'rec':
            self.enc_lstm = ConvLSTM(channels)
            self.dec_lstm = ConvLSTM(channels)
        elif self.conv_type == 'attn':
            self.s_attn_a = AttentionBlock(channels)
            self.s_attn_s = AttentionBlock(channels)
            self.t_attn_a = Attention(channels)
            self.t_attn_s = Attention(channels)
            #self.s_attn_a = Attention(channels)
            #self.s_attn_s = Attention(channels)
            
        self.updated = False
        self.noMeasure = noMeasure
        # include two average meter to measure time
        
    def forward(self, x, rae_hidden=None, rpm_hidden=None, RPM_flag=False):
        # update only once during testing
        if not self.updated and not self.training:
            self.entropy_bottleneck.update(force=True)
            self.updated = True
            
        if not self.noMeasure:
            self.enc_t = self.dec_t = 0
        
        # latent states
        if self.conv_type == 'rec':
            state_enc, state_dec = torch.split(rae_hidden.to(x.device),self.channels*2,dim=1)
            
        # Time measurement: start
        if not self.noMeasure:
            t_0 = time.perf_counter()
            
        # compress
        x = self.gdn1(self.enc_conv1(x))
        x = self.gdn2(self.enc_conv2(x))
        
        if self.conv_type == 'rec':
            x, state_enc = self.enc_lstm(x, state_enc)
        elif self.conv_type == 'attn':
            # use attention
            B,C,H,W = x.size()
            x = self.s_attn_a(x)
            x = self.t_attn_a(x)
            
        x = self.gdn3(self.enc_conv3(x))
        latent = self.enc_conv4(x) # latent optical flow
        
        # Time measurement: end
        if not self.noMeasure:
            self.enc_t += time.perf_counter() - t_0
        
        # quantization + entropy coding
        if self.entropy_type == 'base':
            if self.noMeasure:
                latent_hat, likelihoods = self.entropy_bottleneck(latent, training=self.training)
                if not self.training:
                    latent_string = self.entropy_bottleneck.compress(latent)
            else:
                # encoding
                t_0 = time.perf_counter()
                latent_string = self.entropy_bottleneck.compress(latent)
                self.entropy_bottleneck.enc_t = time.perf_counter() - t_0
                # decoding
                t_0 = time.perf_counter()
                latent_hat = self.entropy_bottleneck.decompress(latent_string, latent.size()[-2:])
                self.entropy_bottleneck.dec_t = time.perf_counter() - t_0
        elif self.entropy_type == 'mshp':
            if self.noMeasure:
                latent_hat, likelihoods = self.entropy_bottleneck(latent, training=self.training)
                if not self.training:
                    latent_string = self.entropy_bottleneck.compress(latent)
            else:
                latent_string, shape = self.entropy_bottleneck.compress_slow(latent)
                latent_hat = self.entropy_bottleneck.decompress_slow(latent_string, shape)
        else:
            self.entropy_bottleneck.set_RPM(RPM_flag)
            if self.noMeasure:
                latent_hat, likelihoods, rpm_hidden = self.entropy_bottleneck(latent, rpm_hidden, training=self.training)
                if not self.training:
                    latent_string = self.entropy_bottleneck.compress(latent)
            else:
                latent_string, _ = self.entropy_bottleneck.compress_slow(latent,rpm_hidden)
                latent_hat, rpm_hidden = self.entropy_bottleneck.decompress_slow(latent_string, latent.size()[-2:], rpm_hidden)
            self.entropy_bottleneck.set_prior(latent)
            
        # add in the time in entropy bottleneck
        if not self.noMeasure:
            self.enc_t += self.entropy_bottleneck.enc_t
            self.dec_t += self.entropy_bottleneck.dec_t
        
        # calculate bpp (estimated) if it is training else it will be set to 0
        if self.noMeasure:
            bits_est = self.entropy_bottleneck.get_estimate_bits(likelihoods)
        else:
            bits_est = torch.FloatTensor([0]).squeeze(0).to(x.device)
        
        # calculate bpp (actual)
        if not self.training:
            bits_act = self.entropy_bottleneck.get_actual_bits(latent_string)
        else:
            bits_act = bits_est

        # Time measurement: start
        if not self.noMeasure:
            t_0 = time.perf_counter()
            
        # decompress
        x = self.igdn1(self.dec_conv1(latent_hat))
        x = self.igdn2(self.dec_conv2(x))
        
        if self.conv_type == 'rec':
            x, state_dec = self.enc_lstm(x, state_dec)
        elif self.conv_type == 'attn':
            # use attention
            B,C,H,W = x.size()
            x = self.s_attn_s(x)
            x = self.t_attn_s(x)
            
        x = self.igdn3(self.dec_conv3(x))
        hat = self.dec_conv4(x)
        
        # Time measurement: end
        if not self.noMeasure:
            self.dec_t += time.perf_counter() - t_0
        
        # auxilary loss
        aux_loss = self.entropy_bottleneck.loss()/self.channels
        
        if self.conv_type == 'rec':
            rae_hidden = torch.cat((state_enc, state_dec),dim=1)
            
        return hat, rae_hidden, rpm_hidden, bits_act, bits_est, aux_loss
            
    def compress_sequence(self,x):
        bs,c,h,w = x.size()
        x_est = torch.FloatTensor([0]).squeeze(0).cuda()
        x_act = torch.FloatTensor([0]).squeeze(0).cuda()
        x_aux = torch.FloatTensor([0]).squeeze(0).cuda()
        rpm_hidden = torch.zeros(1,self.channels*2,h//16,w//16)
        rae_hidden = torch.zeros(1,self.channels*4,h//4,w//4)
        x_hat_list = []
        for frame_idx in range(bs):
            x_i = x[frame_idx,:,:,:].unsqueeze(0)
            x_hat_i,rae_hidden,rpm_hidden,x_act_i,x_est_i,x_aux_i = self.forward(x_i, rae_hidden, rpm_hidden, frame_idx>=1)
            x_hat_list.append(x_hat_i.squeeze(0))
            
            # calculate bpp (estimated) if it is training else it will be set to 0
            x_est += x_est_i.cuda()
            
            # calculate bpp (actual)
            x_act += x_act_i.cuda()
            
            # aux
            x_aux += x_aux_i.cuda()
        x_hat = torch.stack(x_hat_list, dim=0)
        return x_hat,x_act,x_est,x_aux

class MCNet(nn.Module):
    def __init__(self):
        super(MCNet, self).__init__()
        self.l1 = nn.Conv2d(8, 64, kernel_size=3, stride=1, padding=1)
        self.l2 = ResidualBlock(64,64)
        self.l3 = nn.AvgPool2d(kernel_size=2, stride=2, padding=0)
        self.l4 = ResidualBlock(64,64)
        self.l5 = nn.AvgPool2d(kernel_size=2, stride=2, padding=0)
        self.l6 = ResidualBlock(64,64)
        self.l7 = ResidualBlock(64,64)
        self.l8 = nn.Upsample(scale_factor=2, mode='nearest')
        self.l9 = ResidualBlock(64,64)
        self.l10 = nn.Upsample(scale_factor=2, mode='nearest')
        self.l11 = ResidualBlock(64,64)
        self.l12 = nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1)
        self.l13 = nn.Conv2d(64, 3, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        m1 = self.l1(x)
        m2 = self.l2(m1)
        m3 = self.l3(m2)
        m4 = self.l4(m3)
        m5 = self.l5(m4)
        m6 = self.l6(m5)
        m7 = self.l7(m6)
        m8 = self.l8(m7) + m4
        m9 = self.l9(m8)
        m10 = self.l10(m9) + m2
        m11 = self.l11(m10)
        m12 = F.relu(self.l12(m11))
        m13 = self.l13(m12)
        return m13

def get_grid_locations(b, h, w):
    new_h = torch.linspace(-1,1,h).view(-1,1).repeat(1,w)
    new_w = torch.linspace(-1,1,w).repeat(h,1)
    grid  = torch.cat((new_w.unsqueeze(2),new_h.unsqueeze(2)),dim=2)
    grid  = grid.unsqueeze(0)
    grid = grid.repeat(b,1,1,1)
    return grid

def attention(q, k, v, d_model, dropout=None):
    
    scores = torch.matmul(q, k.transpose(-2, -1)) /  math.sqrt(d_model)
        
    scores = F.softmax(scores, dim=-1)
    
    if dropout is not None:
        scores = dropout(scores)
        
    output = torch.matmul(scores, v)
    return output
        
class Attention(nn.Module):
    def __init__(self, d_model, dropout = 0.1):
        super().__init__()
        
        self.d_model = d_model
        
        self.q_linear = nn.Linear(d_model, d_model)
        self.v_linear = nn.Linear(d_model, d_model)
        self.k_linear = nn.Linear(d_model, d_model)
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(d_model, d_model)
    
    def forward(self, x):
        bs,C,H,W = x.size()
        x = x.view(bs,C,-1).permute(2,0,1).contiguous()
        
        # perform linear operation
        
        k = self.k_linear(x)
        q = self.q_linear(x)
        v = self.v_linear(x)
        
        # calculate attention using function we will define next
        scores = attention(q, k, v, self.d_model, self.dropout)
        
        output = self.out(scores) # bs * sl * d_model
        
        output = output.permute(1,2,0).view(bs,C,H,W).contiguous()
    
        return output
        
class AvgNet(nn.Module):
    def __init__(self, d_model):
        super().__init__()
        
        self.d_model = d_model
        
        self.q_linear = nn.Linear(d_model, d_model)
        self.k_linear = nn.Linear(d_model, d_model)
        self.out = nn.Linear(d_model, d_model)
    
    def forward(self, q, k, v):
        
        bs,sl,_ = q.size()
        
        # perform linear operation
        
        k = self.k_linear(k)
        q = self.q_linear(q)
        
        # calculate attention using function we will define next
        scores = torch.matmul(q, k.transpose(-2, -1)) /  math.sqrt(self.d_model)
        ones = torch.ones(bs, sl, sl, dtype=torch.float32)
        diag = torch.eye(sl, dtype=torch.float32)
        tmp = ones - diag # for removing diag elements
        scores = scores*tmp.to(scores.device)
        weights = torch.sum(scores,dim=-1)
        weights = F.softmax(weights,dim=-1).unsqueeze(1)
        
        # qkv:[B,SL,D]
        # weights:[B,SL]
        # out:[B,1,D]
        output = torch.matmul(weights, v)
        
        output = self.out(output.view(bs, self.d_model)) # bs * d_model
    
        return output
        
class VoteNet(nn.Module):
    def __init__(self, channels=128, in_channels=3, kernel=5, padding=2):
        super(VoteNet, self).__init__()
        self.enc = nn.Sequential(nn.Conv2d(in_channels, channels, kernel_size=kernel, stride=2, padding=padding),
                                GDN(channels),
                                nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
                                GDN(channels),
                                nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
                                GDN(channels),
                                nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding)
                                )
        self.dec = nn.Sequential(nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
                                GDN(channels, inverse=True),
                                nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
                                GDN(channels, inverse=True),
                                nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
                                GDN(channels, inverse=True),
                                nn.ConvTranspose2d(channels, in_channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1)
                                )
        #self.s_attn = Attention(channels)
        self.s_attn = AttentionBlock(channels)
        self.t_avg = AvgNet(channels)
        self.channels = channels
        
    def forward(self, x):
        # one option is to treat the first frame as reference
        # input: sequence of frames=[B,3,H,W]
        # output: key frame=[1,C,H,W]
        B,_,H,W = x.size()
        
        # encode original frame to features [B,128,H//16,W//16], e.g., [B,128,14,14]
        y = self.enc(x)
        
        _,_,fH,fW = y.size()
        # spatial attention
        features = self.s_attn(y)
        #features = y.view(B,self.channels,-1).transpose(1,2).contiguous() # B,fH*fW,128
        #features = self.s_attn(features,features,features) # B,fH*fW,128
        
        # temporal attention average
        features = features.view(B,self.channels,-1).permute(2,0,1).contiguous() # fH*fW,B,128
        features = self.t_avg(features,features,features) # fH*fW,128
        features = features.permute(0,1).contiguous().view(1,self.channels,fH,fW)

        # decode attended features to original size [1,3,H,W]
        x_hat = self.dec(features)
        
        return x_hat
        
def set_model_grad(model,requires_grad=True):
    for k,v in model.named_parameters():
        v.requires_grad = requires_grad
    
class CoderSeqOneSeq(CompressionModel):
    def __init__(self, channels=128, kernel=5, padding=2):
        super(CoderSeqOneSeq, self).__init__(channels)
        
        self.g_mv_a = nn.Sequential(
            nn.Conv2d(2, channels, kernel_size=kernel, stride=2, padding=padding),
            GDN(channels),
            nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
            GDN(channels),
            AttentionBlock(channels),
            Attention(channels),
            nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
            GDN(channels),
            nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
        )
        
        self.g_i_a = nn.Sequential(
            nn.Conv2d(3, channels, kernel_size=kernel, stride=2, padding=padding),
            GDN(channels),
            nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
            GDN(channels),
            AttentionBlock(channels),
            Attention(channels),
            nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
            GDN(channels),
            nn.Conv2d(channels, channels, kernel_size=kernel, stride=2, padding=padding),
        )
        
        self.g_s = nn.Sequential(
            nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
            GDN(channels, inverse=True),
            nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
            GDN(channels, inverse=True),
            AttentionBlock(channels),
            Attention(channels),
            nn.ConvTranspose2d(channels, channels, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
            GDN(channels, inverse=True),
            nn.ConvTranspose2d(channels, 3, kernel_size=kernel, stride=2, padding=padding, output_padding=1),
        )
        
        self.h_a = nn.Sequential(
            nn.Conv2d(channels, channels, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(inplace=True),
            AttentionBlock(channels),
            Attention(channels),
            nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
        )

        self.h_s = nn.Sequential(
            nn.ConvTranspose2d(channels, channels, kernel_size=5, stride=2, padding=2),
            nn.LeakyReLU(inplace=True),
            AttentionBlock(channels),
            Attention(channels),
            nn.ConvTranspose2d(channels, channels * 3 // 2, kernel_size=5, stride=2, padding=2, output_padding=1),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(channels * 3 // 2, channels, kernel_size=3, stride=1, padding=1)
        )
        
        self.entropy_parameters = nn.Sequential(
            nn.Conv2d(channels * 2, channels, 1),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(channels, channels, 1),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(channels, channels * 2, 1),
        )
        
        self.optical_flow = OpticalFlowNet()
        self.channels = channels

    def forward(self, x):
        # encode
        # compress the I frame
        # decode I frame
        i_hat, i_est, img_loss, i_aux, i_act, psnr, msssim = I_compression(x[:1],I_level=27)
        if x.size(0)==1:
            return i_hat,i_act,i_est,i_aux
        # derive motions
        mv, l0, l1, l2, l3, l4 = self.optical_flow(x[:-1], x[1:])
        # compress motions in a batch
        # decode motions
        mv_y = self.g_mv_a(mv)
        mv_z = self.h_a(mv_y)
        mv_z_hat, mv_z_likelihood = self.entropy_bottleneck(mv_z)
        if not self.training:
            mv_z_string = self.entropy_bottleneck.compress(mv_z)
        mv_y_hat = self.h_s(mv_z_hat) # context from motion [B-1,C,H//16,W//16]
        
        # calculate bpp (estimated)
        mv_est = get_estimate_bits(None,mv_z_likelihood)
        bits_est = i_est + mv_est
        
        # calculate bpp (actual)
        if not self.training:
            mv_act = get_actual_bits(None,mv_z_string)
        else:
            mv_act = mv_est
        bits_act = i_act + mv_act
            
        # auxilary loss
        aux_loss = self.entropy_bottleneck.loss()/self.channels + i_aux
        
        N = mv.size(0)
        # use decoded motions to reconstruct frames recursively
        ref_frame = i_hat.detach()
        frame_list = [i_hat]
        
        for f_idx in range(N):
            ref_y = self.g_i_a(ref_frame) # feature of I frame [1,C,H//16,W//16]
            gaussian_params = self.entropy_parameters(torch.cat((ref_y,mv_y_hat[f_idx:f_idx+1]), dim=1))
            sigma, mu = torch.split(gaussian_params, self.channels, dim=1)
            y_hat = self.reparameterize(mu, sigma)
            x_hat = self.g_s(y_hat)
            ref_frame = x_hat.detach()
            frame_list.append(x_hat)
        com_frames = torch.cat(frame_list,dim=0)
        psnr = PSNR(x,com_frames,use_list=True)
        psnr = [float(p) for p in psnr]
        print(psnr)
        return com_frames,bits_act,bits_est,aux_loss
        
    def reparameterize(self, mu, sigma):
        """
        Will a single z be enough ti compute the expectation
        for the loss??
        :param mu: (Tensor) Mean of the latent Gaussian
        :param sigma: (Tensor) Standard deviation of the latent Gaussian
        :return:
        """
        std = torch.exp(0.5 * sigma)
        eps = torch.randn_like(std)
        return eps * std + mu
        
def motion_compensation(mc_model,x,motion):
    bs, c, h, w = x.size()
    loc = get_grid_locations(bs, h, w).to(motion.device)
    warped_frames = F.grid_sample(x.to(motion.device), loc + motion.permute(0,2,3,1), align_corners=True)
    MC_input = torch.cat((motion, x.to(motion.device), warped_frames), axis=1)
    MC_frames = mc_model(MC_input)
    return MC_frames,warped_frames
        
class SPVC(nn.Module):
    def __init__(self, name, channels=128, noMeasure=True):
        super(SPVC, self).__init__()
        self.name = name 
        device = torch.device('cuda')
        self.optical_flow = OpticalFlowNet()
        self.MC_network = MCNet()
        self.mv_codec = Coder2D('attn', in_channels=2, channels=channels, kernel=3, padding=1, noMeasure=noMeasure)
        self.res_codec = Coder2D('attn', in_channels=3, channels=channels, kernel=5, padding=2, noMeasure=noMeasure)
        self.channels = channels
        init_training_params(self)
        # split on multi-gpus
        self.split()
        self.noMeasure = noMeasure

    def split(self):
        self.optical_flow.cuda(0)
        self.mv_codec.cuda(1)
        self.MC_network.cuda(1)
        self.res_codec.cuda(1)
        
    def forward(self, x, use_psnr=True):
        bs, c, h, w = x.size()
        
        ref_frame = x[:1]
        
        # init time measurement
        if not self.noMeasure:
            self.enc_t = [];self.dec_t = []
        
        # BATCH:compute optical flow
        t_0 = time.perf_counter()
        mv_tensors, l0, l1, l2, l3, l4 = self.optical_flow(x[:-1], x[1:])
        if not self.noMeasure:
            self.enc_t += [time.perf_counter() - t_0]
        
        # BATCH:compress optical flow
        mv_hat,rae_mv_hidden,rpm_mv_hidden,mv_act,mv_est,mv_aux = self.mv_codec(mv_tensors.cuda(1))
        if not self.noMeasure:
            self.enc_t += [self.mv_codec.enc_t]
            self.dec_t += [self.mv_codec.dec_t]
        
        # SEQ:motion compensation
        t_0 = time.perf_counter()
        MC_frame_list = []
        warped_frame_list = []
        for i in range(x.size(0)-1):
            MC_frame,warped_frame = motion_compensation(self.MC_network,ref_frame,mv_hat[i:i+1])
            # using compensated frame as reference increases the error
            ref_frame = MC_frame.detach()
            MC_frame_list.append(MC_frame)
            warped_frame_list.append(warped_frame)
        MC_frames = torch.cat(MC_frame_list,dim=0)
        warped_frames = torch.cat(warped_frame_list,dim=0)
        mc_loss = calc_loss(x[1:], MC_frames, self.r, use_psnr)
        warp_loss = calc_loss(x[1:], warped_frames, self.r, use_psnr)
        t_comp = time.perf_counter() - t_0
        if not self.noMeasure:
            self.enc_t += [t_comp]
            self.dec_t += [t_comp]
        
        # BATCH:compress residual
        res_tensors = x[1:].to(MC_frames.device) - MC_frames
        res_hat,_, _,res_act,res_est,res_aux = self.res_codec(res_tensors)
        if not self.noMeasure:
            self.enc_t += [self.res_codec.enc_t]
            self.dec_t += [self.res_codec.dec_t]
        
        # reconstruction
        t_0 = time.perf_counter()
        com_frames = torch.clip(res_hat + MC_frames, min=0, max=1).to(x.device)
        if not self.noMeasure:
            self.dec_t += [time.perf_counter() - t_0]
        ##### compute bits
        # estimated bits
        bpp_est = (mv_est.cuda(0) + res_est.cuda(0))/(h * w * bs)
        # actual bits
        bpp_act = (mv_act.cuda(0) + res_act.cuda(0))/(h * w * bs)
        #print(float(ref_est),float(mv_est),float(res_est),float(ref_act),float(mv_act),float(res_act))
        # auxilary loss
        aux_loss = (mv_aux.cuda(0) + res_aux.cuda(0))/2
        # calculate metrics/loss
        psnr = PSNR(x[1:], com_frames, use_list=True)
        msssim = MSSSIM(x[1:], com_frames, use_list=True)
        rec_loss = calc_loss(x[1:], com_frames, self.r, use_psnr)
        flow_loss = (l0+l1+l2+l3+l4).cuda(0)/5*1024
        img_loss = (self.r_rec*rec_loss + \
                    self.r_warp*warp_loss + \
                    self.r_mc*mc_loss + \
                    self.r_flow*flow_loss)
        if not self.noMeasure:
            print(np.sum(self.enc_t)/bs,np.sum(self.dec_t)/bs,self.enc_t,self.dec_t)
        
        return com_frames, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
    
    def loss(self, pix_loss, bpp_loss, aux_loss, app_loss=None):
        loss = self.r_img*pix_loss.cuda(0) + self.r_bpp*bpp_loss.cuda(0) + self.r_aux*aux_loss.cuda(0)
        if app_loss is not None:
            loss += self.r_app*app_loss.cuda(0)
        return loss
        
    def init_hidden(self, h, w):
        return None
         
# conditional coding
class SCVC(nn.Module):
    def __init__(self, name, channels=64, channels2=96, noMeasure=True):
        super(SCVC, self).__init__()
        self.name = name 
        device = torch.device('cuda')
        self.optical_flow = OpticalFlowNet()
        self.mv_codec = Coder2D('attn', in_channels=2, channels=channels, kernel=3, padding=1, noMeasure=noMeasure)
        self.MC_network = MCNet()
        self.ctx_encoder = nn.Sequential(nn.Conv2d(3+channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels2, kernel_size=5, stride=2, padding=2)
                                        )
        self.ctx_decoder1 = nn.Sequential(nn.ConvTranspose2d(channels2, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        GDN(channels, inverse=True),
                                        nn.ConvTranspose2d(channels, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        GDN(channels, inverse=True),
                                        ResidualBlock(channels,channels),
                                        nn.ConvTranspose2d(channels, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        GDN(channels, inverse=True),
                                        ResidualBlock(channels,channels),
                                        nn.ConvTranspose2d(channels, channels, kernel_size=3, stride=2, padding=1, output_padding=1),
                                        )
        self.ctx_decoder2 = nn.Sequential(nn.Conv2d(channels*2, channels, kernel_size=3, stride=1, padding=1),
                                        ResidualBlock(channels,channels),
                                        ResidualBlock(channels,channels),
                                        nn.Conv2d(channels, 3, kernel_size=3, stride=1, padding=1)
                                        )
        self.feature_extract = nn.Sequential(nn.Conv2d(3, channels, kernel_size=3, stride=1, padding=1),
                                        ResidualBlock(channels,channels)
                                        )
        self.tmp_prior_encoder = nn.Sequential(nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels, kernel_size=5, stride=2, padding=2),
                                        GDN(channels),
                                        nn.Conv2d(channels, channels2, kernel_size=5, stride=2, padding=2)
                                        )
        self.entropy_bottleneck = JointAutoregressiveHierarchicalPriors(channels2,useAttention=True)
        self.channels = channels
        init_training_params(self)
        # split on multi-gpus
        self.split()
        self.updated = False
        self.noMeasure = noMeasure

    def split(self):
        self.optical_flow.cuda(0)
        self.mv_codec.cuda(0)
        self.feature_extract.cuda(0)
        self.MC_network.cuda(0)
        self.tmp_prior_encoder.cuda(1)
        self.ctx_encoder.cuda(1)
        self.entropy_bottleneck.cuda(1)
        self.ctx_decoder1.cuda(1)
        self.ctx_decoder2.cuda(1)
        
    def forward(self, x, use_psnr=True):
        if not self.updated and not self.training:
            self.entropy_bottleneck.update(force=True)
            self.updated = True
        # x=[B,C,H,W]: input sequence of frames
        bs, c, h, w = x.size()
        
        ref_frame = x[:1]
        
        # BATCH:compute optical flow
        t_0 = time.perf_counter()
        mv_tensors, l0, l1, l2, l3, l4 = self.optical_flow(x[:-1], x[1:])
        t_flow = time.perf_counter() - t_0
        #print('Flow:',t_flow)
        
        # BATCH:compress optical flow
        t_0 = time.perf_counter()
        mv_hat,rae_mv_hidden,rpm_mv_hidden,mv_act,mv_est,mv_aux = self.mv_codec(mv_tensors)
        t_mv = time.perf_counter() - t_0
        #print('MV entropy:',t_mv)
        
        # SEQ:motion compensation
        t_0 = time.perf_counter()
        MC_frame_list = []
        warped_frame_list = []
        for i in range(x.size(0)-1):
            MC_frame,warped_frame = motion_compensation(self.MC_network,ref_frame,mv_hat[i:i+1])
            ref_frame = MC_frame.detach()
            MC_frame_list.append(MC_frame)
            warped_frame_list.append(warped_frame)
        MC_frames = torch.cat(MC_frame_list,dim=0)
        warped_frames = torch.cat(warped_frame_list,dim=0)
        mc_loss = calc_loss(x[1:], MC_frames, self.r, use_psnr)
        warp_loss = calc_loss(x[1:], warped_frames, self.r, use_psnr)
        t_comp = time.perf_counter() - t_0
        #print('Compensation:',t_comp)
        
        t_0 = time.perf_counter()
        # BATCH:extract context
        context = self.feature_extract(MC_frames).cuda(1)
        
        # BATCH:temporal prior
        prior = self.tmp_prior_encoder(context)
        
        # contextual encoder
        y = self.ctx_encoder(torch.cat((x[1:].cuda(1), context), axis=1))
        t_ctx = time.perf_counter() - t_0
        #print('Context:',t_ctx)
        
        # entropy model
        t_0 = time.perf_counter()
        y_hat, likelihoods = self.entropy_bottleneck(y, prior, training=self.training)
        y_est = self.entropy_bottleneck.get_estimate_bits(likelihoods)
        if not self.training:
            y_string = self.entropy_bottleneck.compress(y)
            y_act = self.entropy_bottleneck.get_actual_bits(y_string)
        else:
            y_act = y_est
        y_aux = self.entropy_bottleneck.loss()/self.channels
        t_y = time.perf_counter() - t_0
        #print('Y entropy:',t_y)
        
        # contextual decoder
        t_0 = time.perf_counter()
        x_hat = self.ctx_decoder1(y_hat)
        x_hat = self.ctx_decoder2(torch.cat((x_hat, context), axis=1)).to(x.device)
        t_ctx_dec = time.perf_counter() - t_0
        #print('Context dec:',t_ctx_dec)
        
        # estimated bits
        bpp_est = (mv_est + y_est.to(mv_est.device))/(h * w * bs)
        # actual bits
        bpp_act = (mv_act + y_act.to(mv_act.device))/(h * w * bs)
        # auxilary loss
        aux_loss = (mv_aux + y_aux.to(mv_aux.device))/2
        # calculate metrics/loss
        psnr = PSNR(x[1:], x_hat.to(x.device), use_list=True)
        msssim = MSSSIM(x[1:], x_hat.to(x.device), use_list=True)
        rec_loss = calc_loss(x[1:], x_hat.to(x.device), self.r, use_psnr)
        img_loss = self.r_warp*warp_loss + \
                    self.r_mc*mc_loss + \
                    self.r_rec*rec_loss
        
        return x_hat, bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
    
    def loss(self, pix_loss, bpp_loss, aux_loss, app_loss=None):
        loss = self.r_img*pix_loss.cuda(0) + self.r_bpp*bpp_loss.cuda(0) + self.r_aux*aux_loss.cuda(0)
        if app_loss is not None:
            loss += self.r_app*app_loss.cuda(0)
        return loss
        
    def init_hidden(self, h, w):
        return None
        
class AE3D(nn.Module):
    def __init__(self, name, noMeasure=True):
        super(AE3D, self).__init__()
        self.name = name 
        device = torch.device('cuda')
        self.conv1 = nn.Sequential(
            nn.Conv3d(3, 64, kernel_size=5, stride=(1,2,2), padding=2), 
            nn.BatchNorm3d(64),
            nn.ReLU(inplace=True),
            nn.Conv3d(64, 128, kernel_size=5, stride=(1,2,2), padding=2), 
            nn.BatchNorm3d(128),
            nn.ReLU(inplace=True),
        )
        self.conv2 = nn.Sequential(
            ResBlockB(),
            ResBlockB(),
            ResBlockB(),
            ResBlockB(),
            ResBlockB(),
            ResBlockA(),
        )
        self.conv3 = nn.Sequential(
            nn.Conv3d(128, 32, kernel_size=5, stride=(1,2,2), padding=2), 
            nn.BatchNorm3d(32),
            nn.ReLU(inplace=True),
        )
        self.entropy_bottleneck = RecProbModel(32)
        self.deconv1 = nn.Sequential( 
            nn.ConvTranspose3d(32, 128, kernel_size=5, stride=(1,2,2), padding=2, output_padding=(0,1,1)),
            nn.BatchNorm3d(128),
            nn.ReLU(inplace=True),
        )
        self.deconv2 = nn.Sequential(
            ResBlockB(),
            ResBlockB(),
            ResBlockB(),
            ResBlockB(),
            ResBlockB(),
            ResBlockA(),
        )
        self.deconv3 = nn.Sequential( 
            nn.ConvTranspose3d(128, 64, kernel_size=5, stride=(1,2,2), padding=2, output_padding=(0,1,1)),
            nn.BatchNorm3d(64),
            nn.ReLU(inplace=True),
            nn.ConvTranspose3d(64, 3, kernel_size=5, stride=(1,2,2), padding=2, output_padding=(0,1,1)),
            nn.BatchNorm3d(3),
        )
        self.channels = 128
        init_training_params(self)
        self.r = 1024 # PSNR:[256,512,1024,2048] MSSSIM:[8,16,32,64]
        # split on multi-gpus
        self.split()
        self.updated = False
        self.noMeasure = noMeasure

    def split(self):
        # too much on cuda:0
        self.conv1.cuda(0)
        self.conv2.cuda(0)
        self.conv3.cuda(0)
        self.deconv1.cuda(1)
        self.deconv2.cuda(1)
        self.deconv3.cuda(1)
        self.entropy_bottleneck.cuda(0)
        
    def forward(self, x, RPM_flag=False, use_psnr=True):
        if not self.updated and not self.training:
            self.entropy_bottleneck.update(force=True)
            self.updated = True
            
        if not self.noMeasure:
            self.enc_t = [];self.dec_t = []
            
        # x=[B,C,H,W]: input sequence of frames
        x = x.permute(1,0,2,3).contiguous().unsqueeze(0)
        bs, c, t, h, w = x.size()
        
        # encoder
        t_0 = time.perf_counter()
        x1 = self.conv1(x)
        x2 = self.conv2(x1) + x1
        latent = self.conv3(x2)
        if not self.noMeasure:
            self.enc_t += [time.perf_counter() - t_0]
        
        # entropy
        # compress each frame sequentially
        bits_est = torch.FloatTensor([0]).squeeze(0).cuda(0)
        bits_act = torch.FloatTensor([0]).squeeze(0).cuda(0)
        rpm_hidden = torch.zeros(1,64,h//8,w//8).cuda()
        latent_hat_list = []
        for frame_idx in range(t):
            latent_i = latent[:,:,frame_idx,:,:]
            self.entropy_bottleneck.set_RPM(frame_idx>=1)
            if self.noMeasure:
                latent_i_hat, likelihoods, rpm_hidden = self.entropy_bottleneck(latent_i, rpm_hidden, training=self.training)
            
                # calculate bpp (estimated) if it is training else it will be set to 0
                bits_est += self.entropy_bottleneck.get_estimate_bits(likelihoods)
            
                # calculate bpp (actual)
                if not self.training:
                    latent_i_string = self.entropy_bottleneck.compress(latent_i)
                    bits_act += self.entropy_bottleneck.get_actual_bits(latent_i_string)
                else:
                    bits_act = bits_est
            else:
                latent_i_string, _ = self.entropy_bottleneck.compress_slow(latent_i,rpm_hidden)
                latent_i_hat, rpm_hidden = self.entropy_bottleneck.decompress_slow(latent_i_string, latent_i.size()[-2:], rpm_hidden)
                bits_act += self.entropy_bottleneck.get_actual_bits(latent_i_string)
                self.enc_t += [self.entropy_bottleneck.enc_t]
                self.dec_t += [self.entropy_bottleneck.dec_t]
            self.entropy_bottleneck.set_prior(latent_i)
            latent_hat_list.append(latent_i_hat)
        latent_hat = torch.stack(latent_hat_list, dim=2)
        
        # decoder
        t_0 = time.perf_counter()
        x3 = self.deconv1(latent_hat.cuda(1))
        x4 = self.deconv2(x3) + x3
        x_hat = self.deconv3(x4)
        if not self.noMeasure:
            self.dec_t += [time.perf_counter() - t_0]
        
        # reshape
        x = x.permute(0,2,1,3,4).contiguous().squeeze(0)
        x_hat = x_hat.permute(0,2,1,3,4).contiguous().squeeze(0)
        
        # estimated bits
        bpp_est = bits_est/(h * w * t)
        
        # actual bits
        bpp_act = bits_act/(h * w * t)
        
        # auxilary loss
        aux_loss = self.entropy_bottleneck.loss()/32
        
        # calculate metrics/loss
        psnr = PSNR(x, x_hat.to(x.device), use_list=True)
        msssim = MSSSIM(x, x_hat.to(x.device), use_list=True)
        
        # calculate img loss
        img_loss = calc_loss(x, x_hat.to(x.device), self.r, use_psnr)
        
        if not self.noMeasure:
            print(np.sum(self.enc_t)/bs,np.sum(self.dec_t)/bs,self.enc_t,self.dec_t)
        
        return x_hat.cuda(0), bpp_est, img_loss, aux_loss, bpp_act, psnr, msssim
    
    def init_hidden(self, h, w):
        return None
        
    def loss(self, pix_loss, bpp_loss, aux_loss, app_loss=None):
        loss = self.r_img*pix_loss.cuda(0) + self.r_bpp*bpp_loss.cuda(0) + self.r_aux*aux_loss.cuda(0)
        if app_loss is not None:
            loss += self.r_app*app_loss.cuda(0)
        return loss
        
class ResBlockA(nn.Module):
    "A ResNet-like block with the GroupNorm normalization providing optional bottle-neck functionality"
    def __init__(self, ch=128, k_size=3, stride=1, p=1):
        super(ResBlockA, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv3d(ch, ch, kernel_size=k_size, stride=stride, padding=p), 
            nn.BatchNorm3d(ch),
            nn.ReLU(inplace=True),

            nn.Conv3d(ch, ch, kernel_size=k_size, stride=stride, padding=p),  
            nn.BatchNorm3d(ch),
        )
        
    def forward(self, x):
        out = self.conv(x) + x
        return out
        
class ResBlockB(nn.Module):
    def __init__(self, ch=128, k_size=3, stride=1, p=1):
        super(ResBlockB, self).__init__()
        self.conv = nn.Sequential(
            ResBlockA(ch, k_size, stride, p), 
            ResBlockA(ch, k_size, stride, p), 
            ResBlockA(ch, k_size, stride, p), 
        )
        
    def forward(self, x):
        out = self.conv(x) + x
        return out
        
def test_batch_proc(name = 'SPVC'):
    print('test',name)
    batch_size = 5
    h = w = 224
    channels = 64
    x = torch.randn(batch_size,3,h,w).cuda()
    if name == 'SPVC':
        model = SPVC(name,channels,noMeasure=False)
    elif name == 'SCVC':
        model = SCVC(name,channels,noMeasure=False)
    elif name == 'AE3D':
        model = AE3D(name,noMeasure=False)
    else:
        print('Not implemented.')
    import torch.optim as optim
    from tqdm import tqdm
    parameters = set(p for n, p in model.named_parameters())
    optimizer = optim.Adam(parameters, lr=1e-4)
    timer = AverageMeter()
    train_iter = tqdm(range(0,2))
    model.eval()
    for i,_ in enumerate(train_iter):
        optimizer.zero_grad()
        
        # measure start
        t_0 = time.perf_counter()
        com_frames, bpp_est, img_loss, aux_loss, bpp_act, psnr, sim = model(x)
        d = time.perf_counter() - t_0
        timer.update(d/(batch_size-1))
        # measure end
        
        loss = model.loss(img_loss,bpp_est,aux_loss)
        loss.backward()
        optimizer.step()
        
        train_iter.set_description(
            f"Batch: {i:4}. "
            f"loss: {float(loss):.2f}. "
            f"img_loss: {float(img_loss):.2f}. "
            f"bits_est: {float(bpp_est):.2f}. "
            f"bits_act: {float(bpp_act):.2f}. "
            f"aux_loss: {float(aux_loss):.2f}. "
            f"duration: {timer.avg:.3f}. ")
            
def test_seq_proc(name='RLVC'):
    print('test',name)
    batch_size = 1
    h = w = 224
    x = torch.rand(batch_size,3,h,w).cuda()
    if name == 'DCVC' or name == 'DCVC_v2':
        model = DCVC(name,noMeasure=False)
    else:
        model = LearnedVideoCodecs(name,noMeasure=False)
    import torch.optim as optim
    from tqdm import tqdm
    parameters = set(p for n, p in model.named_parameters())
    optimizer = optim.Adam(parameters, lr=1e-4)
    timer = AverageMeter()
    hidden_states = model.init_hidden(h,w)
    train_iter = tqdm(range(0,13))
    model.eval()
    x_hat_prev = x
    for i,_ in enumerate(train_iter):
        optimizer.zero_grad()
        
        # measure start
        t_0 = time.perf_counter()
        x_hat, hidden_states, bpp_est, img_loss, aux_loss, bpp_act, p,m = model(x, x_hat_prev.detach(), hidden_states, i%13!=0)
        d = time.perf_counter() - t_0
        timer.update(d)
        # measure end
        
        x_hat_prev = x_hat
        
        loss = model.loss(img_loss,bpp_est,aux_loss)
        loss.backward()
        optimizer.step()
        
        train_iter.set_description(
            f"Batch: {i:4}. "
            f"loss: {float(loss):.2f}. "
            f"img_loss: {float(img_loss):.2f}. "
            f"bpp_est: {float(bpp_est):.2f}. "
            f"bpp_act: {float(bpp_act):.2f}. "
            f"aux_loss: {float(aux_loss):.2f}. "
            f"psnr: {float(p):.2f}. "
            f"duration: {timer.avg:.3f}. ")
            
# integrate all codec models
# measure the speed of all codecs
# two types of test
# 1. (de)compress random images, faster
# 2. (de)compress whole datasets, record time during testing 
# need to implement 3D-CNN compression
# ***************each model can have a timer member that counts enc/dec time
# in training, counts total time, in testing, counts enc/dec time
# how to deal with big batch in training? hybrid mode
# update CNN alternatively?
# hope forward coding works good enough, then we dont have to implement ...
    
if __name__ == '__main__':
    test_batch_proc('SPVC')
    test_batch_proc('SCVC')
    test_batch_proc('AE3D')
    test_seq_proc('RLVC')
    test_seq_proc('DCVC')
    #test_seq_proc('DCVC_v2')
    test_seq_proc('DVC')