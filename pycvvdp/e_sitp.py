import torch
import numpy as np

from pycvvdp.utils import SCIELAB_filter
from pycvvdp.video_source import *
from pycvvdp.vq_metric import *

"""
E-SITP metric. Usage is same as the FovVideoVDP metric (see pytorch_examples).
"""
class e_sitp(vq_metric):
    def __init__(self, device=None):
        # Use GPU if available
        if device is None:
            if torch.cuda.is_available() and torch.cuda.device_count()>0:
                self.device = torch.device('cuda:0')
            else:
                self.device = torch.device('cpu')
        else:
            self.device = device
        
        self.sitp = SCIELAB_filter()
        self.colorspace = 'LMShpe'
        
    '''
    The same as `predict` but takes as input fvvdp_video_source_* object instead of Numpy/Pytorch arrays.
    '''
    def predict_video_source(self, vid_source, frame_padding="replicate"):

        # T_vid and R_vid are the tensors of the size (1,3,N,H,W)
        # where:
        # N - the number of frames
        # H - height in pixels
        # W - width in pixels
        # Both images must contain linear absolute luminance values in cd/m^2
        # 
        # We assume the pytorch default NCDHW layout

        _, _, N_frames = vid_source.get_video_size()

        esitp = 0.0
        for ff in range(N_frames):
            T = vid_source.get_test_frame(ff, device=self.device, colorspace=self.colorspace)
            R = vid_source.get_reference_frame(ff, device=self.device, colorspace=self.colorspace)
            
            # LMS_HPE to LMS_HPE_Lin
            T_lms_lin = self.lmshpe_to_lmshpelin(T)
            R_lms_lin = self.lmshpe_to_lmshpelin(R)
            
            # LMS_HPE_Lin to ITP
            T_itp = self.lmshpelin_to_itp(T_lms_lin)
            R_itp = self.lmshpelin_to_itp(R_lms_lin)
            
            # ITP to Spatial ITP
            T_sitp = self.itp_to_sitp(T_itp, self.ppd)
            R_sitp = self.itp_to_sitp(R_itp, self.ppd)
            
            esitp = esitp + self.eitp_fn(T_sitp, R_sitp) / N_frames
        return esitp, None
    
    def lmshpe_to_lmshpelin(self, img):
        lms_lin_pos = img**0.43
        lms_lin_neg = -1*(-img)**0.43
        condition = torch.less(img, 0)
        lms_lin = torch.where(condition, lms_lin_neg, lms_lin_pos)
        return lms_lin
        
    def lmshpelin_to_itp(self, img):
        LMShpelin_to_itp = (
            (0.4,   0.4,    0.2),
            (4.455, -4.851, 0.396),
            (0.8056,    0.3572, -1.1628) )
        mat = torch.as_tensor( LMShpelin_to_itp, dtype=img.dtype, device=img.device)
        ITP = torch.empty_like(img)
        for cc in range(3):
            ITP[...,cc,:,:,:] = torch.sum(img*(mat[cc,:].view(1,3,1,1,1)), dim=-4, keepdim=True)
        return ITP

    def itp_to_sitp(self, img, ppd):
        S_ITP = torch.empty_like(img)
        # Filters are low-dimensional; construct using np
        [k1, k2, k3] = [torch.as_tensor(filter).to(img) for filter in self.sitp.separableFilters(ppd)]
        S_ITP[...,0:,:,:] = self.sitp.separableConv_torch(torch.squeeze(img[...,0,:,:,:]), k1, torch.abs(k1))
        S_ITP[...,1:,:,:] = self.sitp.separableConv_torch(torch.squeeze(img[...,1,:,:,:]), k2, torch.abs(k2))
        S_ITP[...,2:,:,:] = self.sitp.separableConv_torch(torch.squeeze(img[...,2,:,:,:]), k3, torch.abs(k3))
        return S_ITP
        
    def eitp_fn(self, img1, img2):
        mse = torch.mean(torch.sum( (img1 - img2)**2 ))
        return torch.sqrt(mse)

    def short_name(self):
        return "E-SITP"

    def quality_unit(self):
        return "RMSE"

    def get_info_string(self):
        return None

    def set_display_model(self, display_photometry, display_geometry):
        self.ppd = display_geometry.get_ppd()
