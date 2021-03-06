python train.py --dataset ucf101-24 \
	 			--data_cfg cfg/ucf24.data \
	 			--cfg_file cfg/ucf24.cfg \
	 			--n_classes 24 \
	 			--backbone_3d resnext101 \
	 			--backbone_3d_weights weights/resnext-101-kinetics.pth \
	 			--backbone_2d darknet \
	 			--backbone_2d_weights weights/yolo.weights \
	 			# --resume_path /home/monet/research/YOWO/backup/yowo_ucf101-24_16f_best.pth \

# python ./evaluation/Object-Detection-Metrics/pascalvoc.py --gtfolder /home/monet/research/dataset/ucf24/groundtruths_ucf --detfolder /home/monet/research/dataset/ucf24/ucf_detections/detections_0


