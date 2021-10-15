from __future__ import print_function
import os
import sys
import time
import math
import random

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.backends.cudnn as cudnn
from torchvision import datasets, transforms

from datasets import list_dataset
from datasets.ava_dataset import Ava 
from core.optimization import *
from core.optimization_codec import *
from cfg import parser
from core.utils import *
from core.region_loss import RegionLoss, RegionLoss_Ava
from core.model import YOWO, get_fine_tuning_parameters
from codec.models import LearnedVideoCodecs, StandardVideoCodecs


####### Load configuration arguments
# ---------------------------------------------------------------
args  = parser.parse_args()
cfg   = parser.load_config(args)


####### Check backup directory, create if necessary
# ---------------------------------------------------------------
if not os.path.exists(cfg.BACKUP_DIR):
    os.makedirs(cfg.BACKUP_DIR)


####### Create model
seed = int(time.time())
#seed = int(0)
torch.manual_seed(seed)
use_cuda = True
if use_cuda:
    os.environ['CUDA_VISIBLE_DEVICES'] = '0,1' # TODO: add to config e.g. 0,1,2,3
    torch.cuda.manual_seed(seed)
# ---------------------------------------------------------------
model = YOWO(cfg)
model = model.cuda()
model = nn.DataParallel(model) # in multi-gpu case
# print(model)
pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
logging('Total number of trainable parameters: {}'.format(pytorch_total_params))

# codec model .
assert cfg.TRAIN.CODEC_NAME in ['MRLVC-RPM-BPG','MRLVC-RHP-BPG','MRLVC-RGC-BPG','RLVC','DVC','RAW','x264','x265']
if cfg.TRAIN.CODEC_NAME in ['MRLVC-RPM-BPG', 'MRLVC-RHP-BPG','MRLVC-RGC-BPG','RLVC','DVC','RAW']:
    model_codec = LearnedVideoCodecs(cfg.TRAIN.CODEC_NAME)
elif cfg.TRAIN.CODEC_NAME in ['x264','x265']:
    model_codec = StandardVideoCodecs(cfg.TRAIN.CODEC_NAME)
pytorch_total_params = sum(p.numel() for p in model_codec.parameters() if p.requires_grad)
logging('Total number of trainable codec parameters: {}'.format(pytorch_total_params))
pytorch_nonaux_params = sum(p.numel() for n,p in model_codec.named_parameters() if (not n.endswith(".quantiles")) and p.requires_grad)
logging('Total number of trainable non-aux parameters: {}'.format(pytorch_nonaux_params))
pytorch_aux_params = sum(p.numel() for n,p in model_codec.named_parameters() if n.endswith(".quantiles") and p.requires_grad)
logging('Total number of trainable aux parameters: {}'.format(pytorch_aux_params))


####### Create optimizer
# ---------------------------------------------------------------
parameters = [p for n, p in model_codec.named_parameters() if not (n.endswith(".quantiles") or 'RPM' in n)]
aux_parameters = [p for n, p in model_codec.named_parameters() if n.endswith(".quantiles")]
rpm_parameters = [p for n, p in model_codec.named_parameters() if 'RPM' in n]
optimizer = torch.optim.Adam([{'params': parameters},{'params': aux_parameters, 'lr': 1},{'params':rpm_parameters,'lr':1}],
            lr=cfg.TRAIN.LEARNING_RATE, weight_decay=cfg.SOLVER.WEIGHT_DECAY)
# initialize best score
best_score = 0 
best_codec_score = 0

####### Load yowo model
# ---------------------------------------------------------------
assert(cfg.TRAIN.RESUME_PATH)
if cfg.TRAIN.RESUME_PATH:
    print("===================================================================")
    print('loading checkpoint {}'.format(cfg.TRAIN.RESUME_PATH))
    checkpoint = torch.load(cfg.TRAIN.RESUME_PATH)
    best_score = checkpoint['score']
    model.load_state_dict(checkpoint['state_dict'])
    print("Loaded model score: ", checkpoint['score'])
    print("===================================================================")
    del checkpoint
    # try to load codec model 
    if cfg.TRAIN.CODEC_NAME not in ['MRLVC-RPM-BPG', 'MRLVC-RHP-BPG', 'RLVC', 'DVC']:
        print("No need to load for ", cfg.TRAIN.CODEC_NAME)
    elif cfg.TRAIN.RESUME_CODEC_PATH and os.path.isfile(cfg.TRAIN.RESUME_CODEC_PATH):
        print("Loading for ", cfg.TRAIN.CODEC_NAME)
        checkpoint = torch.load(cfg.TRAIN.RESUME_CODEC_PATH)
        cfg.TRAIN.BEGIN_EPOCH = checkpoint['epoch'] + 1
        best_codec_score = checkpoint['score']
        model_codec.load_my_state_dict(checkpoint['state_dict'])
        print(checkpoint['optimizer'])
        exit(0)
        optimizer.load_state_dict(checkpoint['optimizer'])
        print("Loaded model codec score: ", checkpoint['score'])
        if 'misc' in checkpoint:
            print('Other metrics:',misc)
        del checkpoint
    else:
        print("Cannot load model codec", cfg.TRAIN.CODEC_NAME)
    print("===================================================================")


####### Create backup directory if necessary
# ---------------------------------------------------------------
if not os.path.exists(cfg.BACKUP_DIR):
    os.mkdir(cfg.BACKUP_DIR)


####### Data loader, training scheme and loss function are different for AVA and UCF24/JHMDB21 datasets
# ---------------------------------------------------------------
dataset = cfg.TRAIN.DATASET
assert dataset == 'ucf24' or dataset == 'jhmdb21' or dataset == 'ava', 'invalid dataset'

if dataset == 'ava':
    train_dataset = Ava_codec(cfg, split='train', only_detection=False)
    test_dataset  = Ava_codec(cfg, split='val', only_detection=False)

    loss_module   = RegionLoss_Ava(cfg).cuda()

    train = getattr(sys.modules[__name__], 'train_ava_codec')
    test  = getattr(sys.modules[__name__], 'test_ava_codec')



elif dataset in ['ucf24', 'jhmdb21']:
    train_dataset = list_dataset.UCF_JHMDB_Dataset_codec(cfg.LISTDATA.BASE_PTH, cfg.LISTDATA.TRAIN_FILE, dataset=dataset,
                       shape=(cfg.DATA.TRAIN_CROP_SIZE, cfg.DATA.TRAIN_CROP_SIZE),
                       transform=transforms.Compose([transforms.ToTensor()]), 
                       train=True, clip_duration=cfg.DATA.NUM_FRAMES, sampling_rate=cfg.DATA.SAMPLING_RATE)
    test_dataset  = list_dataset.UCF_JHMDB_Dataset_codec(cfg.LISTDATA.BASE_PTH, cfg.LISTDATA.TEST_FILE, dataset=dataset,
                       shape=(cfg.DATA.TRAIN_CROP_SIZE, cfg.DATA.TRAIN_CROP_SIZE),
                       transform=transforms.Compose([transforms.ToTensor()]), 
                       train=False, clip_duration=cfg.DATA.NUM_FRAMES, sampling_rate=cfg.DATA.SAMPLING_RATE)
                       
    loss_module   = RegionLoss(cfg).cuda()

    train = getattr(sys.modules[__name__], 'train_ucf24_jhmdb21_codec')
    test  = getattr(sys.modules[__name__], 'test_ucf24_jhmdb21_codec')


####### Training and Testing Schedule
# ---------------------------------------------------------------
if cfg.TRAIN.EVALUATE:
    logging('evaluating ...')
    test(cfg, 100, model, model_codec, test_dataset, loss_module)
else:
    for epoch in range(cfg.TRAIN.BEGIN_EPOCH, cfg.TRAIN.END_EPOCH + 1):
        # Adjust learning rate
        r = adjust_codec_learning_rate(optimizer, epoch, cfg)
        
        # Train and test model
        logging('training at epoch %d, r=%.2f' % (epoch,r))
        train(cfg, epoch, model, model_codec, train_dataset, loss_module, optimizer)
        if epoch >= 3:
            logging('testing at epoch %d' % (epoch))
            score,misc = test(cfg, epoch, model, model_codec, test_dataset, loss_module)
        else:
            score,misc = 0,()

        # Save the model to backup directory
        is_best = score > best_codec_score
        if is_best:
            print("New best score is achieved: ", score, misc)
            print("Previous score was: ", best_codec_score)
            best_codec_score = score

        state = {
            'epoch': epoch,
            'state_dict': model_codec.state_dict(),
            'optimizer': optimizer.state_dict(),
            'score': score,
            'misc': misc
            }
        save_codec_checkpoint(state, is_best, cfg.BACKUP_DIR, cfg.TRAIN.DATASET, cfg.DATA.NUM_FRAMES, cfg.TRAIN.CODEC_NAME)
        logging('Weights are saved to backup directory: %s' % (cfg.BACKUP_DIR))