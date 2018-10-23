import math
import numpy as np
from data_reader import SlayerParams
import torch
import torch.nn as nn
import torch.nn.functional as F
import slayer_cuda


class SlayerNet(nn.Module):

    def __init__(self, net_params, weights_init = [4,4], device=torch.device('cuda')):
        super(SlayerNet, self).__init__()
        self.net_params = net_params
        self.trainer = SlayerTrainer(net_params, device)
        self.srm = self.trainer.calculate_srm_kernel()
        self.ref = self.trainer.calculate_ref_kernel()
        # Emulate a fully connected 250 -> 25
        self.fc1 = SpikeLinear(250, 25).to(device)
        nn.init.normal_(self.fc1.weight, mean=0, std=weights_init[0])
        # Emulate a fully connected 25 -> 1
        self.fc2 = SpikeLinear(25, 1).to(device)
        nn.init.normal_(self.fc2.weight, mean=0, std=weights_init[1])
        self.device=device

    def forward(self, x):
        # Apply srm to input spikes
        x = self.trainer.apply_srm_kernel(x, self.srm)
        # Linear + activation
        x = self.fc1(x)
        x = SpikeFunc.apply(x, self.net_params, self.ref, self.net_params['af_params']['sigma'][0], self.device)
        # Apply srm to middle layer spikes
        x = self.trainer.apply_srm_kernel(x.view(1,1,1,25,-1), self.srm)
        # # Apply second layer
        x = SpikeFunc.apply(self.fc2(x), self.net_params, self.ref, self.net_params['af_params']['sigma'][1], self.device)
        return x

class SpikeFunc(torch.autograd.Function):

	@staticmethod
	def forward(ctx, multiplied_activations, net_params, ref, sigma, device=torch.device('cuda')):
		# Calculate membrane potentials
		(multiplied_activations, spikes) = SpikeFunc.get_spikes_cuda(multiplied_activations, ref, net_params)
		scale = torch.autograd.Variable(torch.tensor(net_params['pdf_params']['scale'], device=device, dtype=torch.float32), requires_grad=False)
		tau = torch.autograd.Variable(torch.tensor(net_params['pdf_params']['tau'], device=device, dtype=torch.float32), requires_grad=False)
		theta = torch.autograd.Variable(torch.tensor(net_params['af_params']['theta'], device=device, dtype=torch.float32), requires_grad=False)
		ctx.save_for_backward(multiplied_activations, theta, tau, scale)
		return spikes

	@staticmethod
	def backward(ctx, grad_output):
		(membrane_potentials, theta, tau, scale) = ctx.saved_tensors
		# Don't return any gradient for parameters
		return (grad_output * SpikeFunc.calculate_pdf(membrane_potentials, theta, tau, scale), None, None, None, None)

	# Call the cuda wrapper, Note! sigma is not implemented
	@staticmethod
	def get_spikes_cuda(potentials, ref, net_params):
		return slayer_cuda.get_spikes_cuda(potentials, torch.empty_like(potentials), ref, net_params['af_params']['theta'], net_params['t_s'])

	@staticmethod
	def calculate_pdf(membrane_potentials, theta, tau, scale):
		return scale / tau * torch.exp(-torch.abs(membrane_potentials - theta) / tau)

# Helper module to emulate fully connected layers in Spiking Networks
class SpikeLinear(nn.Conv3d):

	def __init__(self, in_features, out_features):
		kernel = (1, in_features, 1)
		super(SpikeLinear, self).__init__(1, out_features, kernel, bias=False)

	def forward(self, input):
		out = F.conv3d(input, self.weight, self.bias, self.stride, self.padding,
					   self.dilation, self.groups)
		return out

class SlayerTrainer(object):

	def __init__(self, net_params, device=torch.device('cuda'), data_type=np.float32):
		self.device = device
		self.net_params = net_params
		# Data type used for membrane potentials, weights
		self.data_type = data_type

	def calculate_srm_kernel(self, num_channels = None):
		if num_channels is None:
			num_channels = self.net_params['input_channels']
		single_kernel = self._calculate_srm_kernel(self.net_params['sr_params']['mult'], self.net_params['sr_params']['tau'],
			self.net_params['sr_params']['epsilon'], self.net_params['t_end'], self.net_params['t_s'])
		concatenated_srm =  self._concatenate_srm_kernel(single_kernel, num_channels)
		return torch.tensor(concatenated_srm, device=self.device)

	# Generate kernels that will act on a single channel (0 outside of diagonals)
	def _concatenate_srm_kernel(self, kernel, n_channels):
		eye_tensor = np.reshape(np.eye(n_channels, dtype = self.data_type), (n_channels, n_channels, 1, 1, 1))
		return kernel * eye_tensor

	def _calculate_srm_kernel(self, mult, tau, epsilon, t_end, t_s):
		srm_kernel = self._calculate_eps_func(mult, tau, epsilon, t_end, t_s)
		# Make sure the kernel is odd size (for convolution)
		if ((len(srm_kernel) % 2) == 0) : srm_kernel.append(0)
		# Convert to numpy array and reshape in a shape compatible for 3d convolution
		srm_kernel = np.asarray(srm_kernel, dtype = self.data_type)
		# Prepend length-1 zeros to make the convolution filter causal
		prepended_zeros = np.zeros((len(srm_kernel)-1,), dtype = self.data_type)
		srm_kernel = np.flip(np.concatenate((prepended_zeros, srm_kernel)))
		return srm_kernel.reshape((1,1,len(srm_kernel)))
		# Convert to pytorch tensor

	def _calculate_eps_func(self, mult, tau, epsilon, t_end, t_s):
		eps_func = []
		for t in np.arange(0, t_end, t_s):
			srm_val = mult * t / tau * math.exp(1 - t / tau)
			# Make sure we break after the peak
			if abs(srm_val) < abs(epsilon) and t > tau:
				break
			eps_func.append(srm_val)
		return eps_func
		
	def calculate_ref_kernel(self):
		ref_kernel = self._calculate_eps_func(self.net_params['ref_params']['mult'], self.net_params['ref_params']['tau'],
			self.net_params['ref_params']['epsilon'], self.net_params['t_end'], self.net_params['t_s'])
		return torch.tensor(ref_kernel, device=self.device)
		
	def apply_srm_kernel(self, input_spikes, srm):
		return F.conv3d(input_spikes, srm, padding=(0,0,int(srm.shape[4]/2))) * self.net_params['t_s']

	def calculate_error_spiketrain(self, a, des_a):
		return a - des_a

	def calculate_l2_loss_spiketrain(self, a, des_a):
		return torch.sum(self.calculate_error_spiketrain(a, des_a) ** 2) / 2 * self.net_params['t_s']

	def calculate_error_classification(self, spikes, des_spikes):
		err = spikes.detach()
		t_valid = self.net_params['t_valid']
		err[:,:,:,:,0:t_valid] = (torch.sum(spikes[:,:,:,:,0:t_valid],4 , keepdim=True) - des_spikes) / (t_valid / self.net_params['t_s'])
		return err
		
	def calculate_l2_loss_classification(self, spikes, des_spikes):
		return torch.sum(self.calculate_error_classification(spikes, des_spikes) ** 2) / 2 * self.net_params['t_s']

	def get_accurate_classifications(self, out_spikes, des_labels):
		output_labels = torch.argmax(torch.sum(out_spikes, 4, keepdim=True), 1)
		correct_labels = sum([1 for (classified, gtruth) in zip(output_labels.flatten(), des_labels.flatten()) if (classified == int(gtruth))])
		return correct_labels