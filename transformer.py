import cv2
import numpy as np
import time
import torch
import glob

from torchvision import transforms
from torch.utils.data import Dataset
from utils import *
from eval_results import *
from cfg import parse_cfg
# todo
# change quality in a tile

dataset = 'ucf101-24'

def tile_disturber(images,rows,cols,r):
	pass


def get_edge_feature(frame, edge_blur_rad=11, edge_blur_var=0, edge_canny_low=101, edge_canny_high=255):
	gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
	start = time.perf_counter()
	blur = cv2.GaussianBlur(gray, (edge_blur_rad, edge_blur_rad), edge_blur_var)
	edge = cv2.Canny(blur, edge_canny_low, edge_canny_high)
	end = time.perf_counter()
	return edge, end-start
    

def get_KAZE_feature(frame):
	alg = cv2.KAZE_create()
	start = time.perf_counter()
	kps = alg.detect(frame)
	end = time.perf_counter()
	kps = sorted(kps, key=lambda x: -x.response)[:32]
	points = [p.pt for p in kps]
	return points, end-start

def get_harris_corner(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)

	start = time.perf_counter()
	dst = cv2.cornerHarris(gray,2,3,0.04)
	end = time.perf_counter()

	# Threshold for an optimal value, it may vary depending on the image.
	dst[dst>0.01*dst.max()]=[255]
	dst[dst<255]=[0]
	return dst, end-start

def get_GFTT(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)

	start = time.perf_counter()
	corners = cv2.goodFeaturesToTrack(gray,25,0.01,10)
	corners = np.int0(corners)
	end = time.perf_counter()
	points = [i.ravel() for i in corners]
	return points, end-start

# pip install opencv-python==3.4.2.16
# pip install opencv-contrib-python==3.4.2.16
def get_SIFT(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)

	sift = cv2.xfeatures2d.SIFT_create()
	start = time.perf_counter()
	kps = sift.detect(gray,None)
	end = time.perf_counter()
	points = [p.pt for p in kps]
	return points, end-start

def get_SURF(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)

	surf = cv2.xfeatures2d.SURF_create()
	start = time.perf_counter()
	kps = surf.detect(gray,None)
	end = time.perf_counter()
	points = [p.pt for p in kps]
	return points, end-start

def get_FAST(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
	start = time.perf_counter()
	fast = cv2.FastFeatureDetector_create(threshold=50)
	kps = fast.detect(img,None)
	end = time.perf_counter()
	points = [p.pt for p in kps]
	return points, end-start

def get_STAR(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
	# Initiate STAR detector
	star = cv2.xfeatures2d.StarDetector_create()

	# find the keypoints with STAR
	start = time.perf_counter()
	kps = star.detect(img,None)
	end = time.perf_counter()
	points = [p.pt for p in kps]
	return points, end-start

def get_ORB(frame):
	img = frame.copy()
	gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)

	orb = cv2.ORB_create()
	start = time.perf_counter()
	kps = orb.detect(img,None)
	end = time.perf_counter()
	points = [p.pt for p in kps]
	return points, end-start


def count_point_ROIs(ROIs, points):
	counter = 0
	for px,py in points:
		inROI = False
		for x1,y1,x2,y2 in ROIs:
			if x1<=px and x2>px and y1<=py and y2>py:
				inROI = True
				break
		if inROI:counter += 1
	return counter*1.0/len(points), 1-counter*1.0/len(points)

def count_map_ROIs(ROIs, mp):
	total_pts = np.count_nonzero(mp)
	for x1,y1,x2,y2 in ROIs:
		mp[y1:y2,x1:x2] = 0
	nonroi_pts = np.count_nonzero(mp)
	return 1-nonroi_pts*1.0/total_pts, nonroi_pts*1.0/total_pts

def ROI_area(ROIs,w,h):
	im = np.zeros((h,w),dtype=np.uint8)
	for x1,y1,x2,y2 in ROIs:
		im[y1:y2,x1:x2] = 1
	roi = np.count_nonzero(im)
	return roi*1.0/(w*h), 1-roi*1.0/(w*h)

# change quality of non-ROI
# r_in is the scaled ratio of ROIs
# r_out is the scaled ratio of the whole image
def region_disturber(image,label,r_in,r_out):
	# get the original content from ROI
	# downsample rest, then upsample
	# put roi back
	means = (104, 117, 123)
	w,h = 320,240
	dsize_out = (int(w*r_out),int(h*r_out))
	crops = []
	for _,cx,cy,imgw,imgh  in label:
		cx=int(cx*320);cy=int(cy*240);imgw=int(imgw*320);imgh=int(imgh*320)
		x1=max(cx-imgw//2,0);x2=min(cx+imgw//2,w);y1=max(cy-imgw//2,0);y2=min(cy+imgw//2,h)
		dsize_in = (int((x2-x1)*r_in),int((y2-y1)*r_in))
		crop = image[y1:y2,x1:x2]
		crop_d = cv2.resize(crop, dsize=dsize_in, interpolation=cv2.INTER_LINEAR)
		crop_u = cv2.resize(crop_d, dsize=(x2-x1,y2-y1), interpolation=cv2.INTER_LINEAR)
		crops.append((x1,y1,x2,y2,crop_u))
	if r_out<1:
		# downsample
		downsample = cv2.resize(image, dsize=dsize_out, interpolation=cv2.INTER_LINEAR)
		# upsample
		image = cv2.resize(downsample, dsize=(w,h), interpolation=cv2.INTER_LINEAR)
	for x1,y1,x2,y2,crop  in crops:
		image[y1:y2,x1:x2] = crop
	
	return image

def path_to_disturbed_image(pil_image, label, r_in, r_out):
	b,g,r = cv2.split(np.array(pil_image))
	np_img = cv2.merge((b,g,r))
	np_img = region_disturber(np_img,label, r_in, r_out)
	pil_image = Image.fromarray(np_img)
	return pil_image


# analyze static and motion feature points
# need to count the number of features ROI and not in ROI
# calculate the density
# should compare  
# percentage of features/percentage of area
def analyzer(images,targets):
	means = (104, 117, 123)
	w,h = 1024,512
	cnt = 0
	avg_dens1,avg_dens2 = np.zeros(9,dtype=np.float64),np.zeros(9,dtype=np.float64)
	for image,label in zip(images,targets):
		for ch in range(0,3):
			image[ch,:,:] += means[2-ch]
		rgb_frame = image.permute(1,2,0).numpy().astype(np.uint8)
		bgr_frame = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2BGR)
		label = label.numpy()
		ROIs  =[]
		for x1,y1,x2,y2,_  in label:
			x1*=1024;x2*=1024;y1*=512;y2*=512
			ROIs.append([int(x1),int(y1),int(x2),int(y2)])
		# edge diff
		edge, _ = get_edge_feature(bgr_frame)
		# *kaze feat
		kaze, _ = get_KAZE_feature(bgr_frame)
		# harris corner
		hc, _ = get_harris_corner(bgr_frame)
		# GFTT
		gftt, _ = get_GFTT(bgr_frame)
		# *SIFT
		sift, _ = get_SIFT(bgr_frame)
		# *SURF
		surf, _ = get_SURF(bgr_frame)
		# FAST
		fast, _ = get_FAST(bgr_frame)
		# STAR
		star, _ = get_STAR(bgr_frame)
		# ORB
		orb, _ = get_ORB(bgr_frame)

		point_features = [gftt, kaze, sift, surf, fast, star, orb]
		map_features = [edge,hc]
		in_roi,out_roi = ROI_area(ROIs,w,h)
		density1,density2 = [],[]
		for mp in map_features:
			c1,c2 = count_map_ROIs(ROIs,mp)
			density1+=['{:0.6f}'.format(c1*1.0/in_roi)]
			density2+=['{:0.6f}'.format(c2*1.0/out_roi)]
		for points in point_features:
			c1,c2 = count_point_ROIs(ROIs,points)
			density1+=['{:0.6f}'.format(c1*1.0/in_roi)]
			density2+=['{:0.6f}'.format(c2*1.0/out_roi)]

		for ch in range(0,3):
			image[ch,:,:] -= means[2-ch]

		cnt += 1
		avg_dens1 += np.array(density1,dtype=np.float64)
		avg_dens2 += np.array(density2,dtype=np.float64)
	return avg_dens1/4,avg_dens2/4

class LRU(OrderedDict):

	def __init__(self, maxsize=128, /, *args, **kwds):
		self.maxsize = maxsize
		super().__init__(*args, **kwds)

	def __getitem__(self, key):
		value = super().__getitem__(key)
		self.move_to_end(key)
		return value

	def __setitem__(self, key, value):
		if key in self:
			self.move_to_end(key)
		super().__setitem__(key, value)
		if len(self) > self.maxsize:
			oldest = next(iter(self))
			del self[oldest]

# define a class for transformation
class Transformer:
	def __init__(self,name):
		# need a dict as buffer to store transformed image of a range
		self.name = name
		self.lru = LRU(16) # size of clip

	def transform(self, image, img=None, label=None, C_param=None, img_index=None):
		# analyze features in image, 
		# derive the quality in each tile based on the compression param

		# downsample the image based on the quality
		if img_index in self.LRU:
			image = self.lru[img_index]
		else:
			image = path_to_disturbed_image(img, label, 0.5, 1)
			self.lru[img_index] = image
		return images

def get_clip(root, imgpath, train_dur, dataset):
	im_split = imgpath.split('/')
	num_parts = len(im_split)
	class_name = im_split[-3]
	file_name = im_split[-2]
	im_ind = int(im_split[num_parts - 1][0:5])
	if dataset == 'ucf101-24':
		img_name = os.path.join(class_name, file_name, '{:05d}.jpg'.format(im_ind))
	elif dataset == 'jhmdb-21':
		img_name = os.path.join(class_name, file_name, '{:05d}.png'.format(im_ind))
	labpath = os.path.join(base_path, 'labels', class_name, file_name, '{:05d}.txt'.format(im_ind))
	img_folder = os.path.join(base_path, 'rgb-images', class_name, file_name)
	max_num = len(os.listdir(img_folder))
	clip = [] 

	for i in reversed(range(train_dur)):
		i_img = im_ind - i * 1
		if i_img < 1:
			i_img = 1
		elif i_img > max_num:
			i_img = max_num

		if dataset == 'ucf101-24':
			path_tmp = os.path.join(base_path, 'rgb-images', class_name, file_name, '{:05d}.jpg'.format(i_img))
		elif dataset == 'jhmdb-21':
			path_tmp = os.path.join(base_path, 'rgb-images', class_name, file_name, '{:05d}.png'.format(i_img)) 

		# read label from file, then apply transformer
		lab_path_tmp = os.path.join(base_path, 'labels', class_name, file_name, '{:05d}.txt'.format(i_img)) 
		pil_image = path_to_disturbed_image(path_tmp, lab_path_tmp,0.5,1)

		clip.append(pil_image.convert('RGB'))

	label = torch.zeros(50 * 5)
	try:
		tmp = torch.from_numpy(read_truths_args(labpath, 8.0 / clip[0].width).astype('float32'))
	except Exception:
		tmp = torch.zeros(1, 5)

	tmp = tmp.view(-1)
	tsz = tmp.numel()

	if tsz > 50 * 5:
		label = tmp[0:50 * 5]
	elif tsz > 0:
		label[0:tsz] = tmp

	return clip, label, img_name

class testData(Dataset):
    def __init__(self, root, shape=None, transform=None, clip_duration=16):

        self.root = root
        if dataset == 'ucf101-24':
            self.label_paths = sorted(glob.glob(os.path.join(root, '*.jpg')))
        elif dataset == 'jhmdb-21':
            self.label_paths = sorted(glob.glob(os.path.join(root, '*.png')))

        self.shape = shape
        self.transform = transform
        self.clip_duration = clip_duration

    def __len__(self):
        return len(self.label_paths)

    def __getitem__(self, index):
        assert index <= len(self), 'index range error'
        label_path = self.label_paths[index]

        clip, label, img_name = get_clip(self.root, label_path, self.clip_duration, dataset)
        clip = [img.resize(self.shape) for img in clip]

        if self.transform is not None:
            clip = [self.transform(img) for img in clip]

        clip = torch.cat(clip, 0).view((self.clip_duration, -1) + self.shape).permute(1, 0, 2, 3)

        return clip, label, img_name

if __name__ == "__main__":
    # img = cv2.imread('/home/bo/research/dataset/ucf24/compressed/000000.jpg')
    # img = cv2.imread('/home/bo/research/dataset/ucf24/rgb-images/Basketball/v_Basketball_g01_c01/00001.jpg')

	use_cuda = True
	kwargs = {'num_workers': 0, 'pin_memory': True} if use_cuda else {}

	datacfg       = 'cfg/ucf24.data'
	cfgfile       = 'cfg/ucf24.cfg'

	net_options   = parse_cfg(cfgfile)[0]
	base_path     = '/home/bo/research/dataset/ucf24'

	clip_duration = int(net_options['clip_duration'])

	line = 'Basketball/v_Basketball_g01_c01'

	test_loader = torch.utils.data.DataLoader(
					testData(os.path.join(base_path, 'rgb-images', line),
					shape=(224, 224), transform=transforms.Compose([
					transforms.ToTensor()]), clip_duration=clip_duration),
					batch_size=1, shuffle=False, **kwargs)
	for batch_idx, (data, target, img_name) in enumerate(test_loader):
		print(data.shape,target.shape)
		if batch_idx==10:break