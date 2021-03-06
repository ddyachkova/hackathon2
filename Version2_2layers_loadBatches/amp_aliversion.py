
from torch.autograd import Variable
import torch_geometric.transforms
from torch_geometric.transforms import knn_graph
import torch_geometric.data
import torch 
from torch_geometric.data import Data
import numpy as np
import pandas as pd
from scipy.spatial import distance_matrix
import torch
import pyarrow as pa
import pyarrow.parquet as pq
import h5py
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from torch_geometric.nn import knn_graph
import os 

from torch.utils.data import *
from functools import partial
import timeit

import argparse
use_cuda = torch.cuda.is_available()
device = torch.device("cuda" if use_cuda else "cpu")


parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, default=42, help='Random seed.')
parser.add_argument('--epochs', type=int, default=50, help='Number of epochs to train.')
parser.add_argument('--batch_size', type=int, default=32, help='Initial learning rate.') #100

parser.add_argument('--lr', type=float, default=0.001, help='Initial learning rate.') #0.001
parser.add_argument('--dropout', type=float, default=0.4, help='Dropout rate (1 - keep probability).')
args = parser.parse_args([])
torch.backends.cudnn.benchmark = True

class ParquetDataset(Dataset):
    def __init__(self, filename):
        self.parquet = pq.ParquetFile(filename)
        self.cols = None 
    def __getitem__(self, index):
        data = self.parquet.read_row_group(index, columns=self.cols).to_pydict()
        data['X_jets'] = np.float32(data['X_jets'][0]) 
        # Preprocessing
        data['X_jets'] = data['X_jets'][1]
        data['X_jets'][data['X_jets'] < 1.e-3] = 0. # Zero-Suppression
        return dict(data)
    def __len__(self):
        return self.parquet.num_row_groups

def jets(i, ecal):
    xhit2,yhit2 = torch.nonzero(ecal[i])[:, 0].cpu().numpy(), torch.nonzero(ecal[i])[:, 1].cpu().numpy()    
    eneEcal=ecal[i].cpu().numpy()[xhit2,yhit2]
    feats=np.transpose(np.vstack((xhit2,yhit2,eneEcal)))  ## concatenate x,y locations and energies (3 features in total)
    cords=feats[:,[0,1]] ## cords = x,y coordinates 
    allFeats=torch.from_numpy(feats).float()  ## features to tensors 
    cords2=torch.from_numpy(cords)  ## coordinates to tensors 
    edge_index = knn_graph(cords2, k=4, batch=None, loop=True)  ## Create knn graph adjacency matrix 
    data=Data(x=allFeats,edge_index=edge_index) ## Create graph data with feature matrix x and adjacency matrix edge_index
    return data

def get_data_loader(datasets, batch_size, cut, random_sampler=False):
    dset = ConcatDataset([ParquetDataset(dataset) for dataset in datasets])
    idxs = np.random.permutation(len(dset))
    if random_sampler: 
        random_sampler = sampler.SubsetRandomSampler(idxs[:cut])
    else: 
        random_sampler = None 
    data_loader = DataLoader(dataset=dset, batch_size=batch_size, shuffle=False, num_workers=10, sampler=random_sampler, pin_memory=True)
    return data_loader


datasets = ['./parquets/Boosted_Jets_Sample-%i.snappy.parquet'%i for i in range(5)]
data_loader = get_data_loader(datasets, args.batch_size, cut = None, random_sampler = True)

def jets(datei,number1,number2):

    cols = None
    graphs=[]

    for i in range(number1,number2):
      #datei['X_jets'] = np.float32(datei['X_jets']) [0] 
      
      #ecal=datei['X_jets'][1]  ## Select ECAL Data out of the 3 available channels

      #ecal[ecal<=1e-3]=0  ## Remove noisy values
      ecal=datei[i].cpu().numpy()

      xhit2,yhit2=np.nonzero(ecal)  ## Select hits in detector

      eneEcal=ecal[xhit2,yhit2]  ## Select energies of hits

      feats=np.transpose(np.vstack((xhit2,yhit2,eneEcal)))  ## concatenate x,y locations and energies (3 features in total)
      
      cords=feats[:,[0,1]] ## cords = x,y coordinates 
      
      allFeats=torch.from_numpy(feats).float()  ## features to tensors 
      
      cords2=torch.from_numpy(cords)  ## coordinates to tensors 
    
      edge_index = knn_graph(cords2, k=4, batch=None, loop=True)  ## Create knn graph adjacency matrix 

      donnees=Data(x=allFeats,edge_index=edge_index) ## Create graph data with feature matrix x and adjacency matrix edge_index
      
      graphs.append(donnees)

    return graphs


import DiffAE

model3=DiffAE.GraphAE()
model3.train()
model3.to(device)#.cuda()

scaler = torch.cuda.amp.GradScaler()

optimizer = torch.optim.Adam(model3.parameters(), lr=args.lr, weight_decay=0.001)

## generate list to count nodes for each graph
def nodeCounter(samples):
    inds=[]
    for k in samples:
        inds.append(k['x'].shape[0])
    return inds

def ref(bsize,nodeC,i1,i2):
  maxC=np.max(np.array(nodeC))
  maxC=maxC + (4 - maxC % 4) ##max num of nodes 1161%4
  refMat=np.zeros((bsize,maxC)) ## matrix of zeros
  for pi in range(i1,i2):##10
    refMat[10-(i2-pi),:nodeC[pi]]=1 ## fill ones 
  return refMat,maxC

def assigner(nodelist):
  fin=[]
  countit=0
  for m in nodelist:
      fin.append(np.repeat(countit,m))
      countit+=1
  return np.array(fin)

from optimizer import loss_function

from torch.cuda.amp import GradScaler, autocast

import time
for epoch in range(50):
      #model.train()
  count=0
  c1,c2=0,args.batch_size
  epLoss=0
  t = time.time()
  for i, data in enumerate(data_loader):
        count+=1
        ecal2 = data['X_jets'].cuda()
        rawGraph=jets(ecal2,0,32) ##Generating graphs from raw data 
        nodeCount=nodeCounter(rawGraph)
        lengs=torch.LongTensor(np.hstack(assigner(np.array(nodeCount[c1:c2])-c1))).cuda()
        
        compress=torch_geometric.data.Batch.from_data_list(rawGraph)

        gra=compress.x.to(device)
        adj=compress.edge_index.to(device)

        refMat,maxCount=ref(args.batch_size,nodeCount,c1,c2)
        
        optimizer.zero_grad()
        mask=torch.from_numpy(refMat).to(device)
        maxi=torch.from_numpy(np.array(maxCount)).to(device)

        r1 ,adj1,mu,sig= model3(gra,adj,lengs.to(device),mask,maxi)

        #sparse=to_sparse_batch(r1, adj1, mask=torch.LongTensor(mask).cuda())

        ##loss = loss_function(r1,gra,lengs,refMat,mu,sig)    



        with torch.cuda.amp.autocast(): 
            loss = loss_function(r1,gra,lengs,refMat,mu,sig)  
      
        # Scales the loss, and calls backward() 
        # to create scaled gradients 
        scaler.scale(loss).backward() 
      
        # Unscales gradients and calls 
        # or skips optimizer.step() 
        scaler.step(optimizer) 
      
        # Updates the scale for next iteration 
        scaler.update() 



        ##loss.backward()

        ##optimizer.step()

        cur_loss = loss.item()
        
        epLoss+=float(cur_loss)

        #c1+=args.batch_size
        #c2+=args.batch_size
        
        if count%2200==0:
            print("Epoch:", '%04d' % (epoch + 1), "train_loss=", "{:.5f}".format(epLoss/count),"time=", "{:.5f}".format(time.time() - t))
            t = time.time()

  torch.save({
        'epoch': epoch,
        'model_state_dict': model3.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'epoch':epoch,
        'loss': loss,
        'epLoss':epLoss
        }, './amp_loadBatches_b128_nw8.pth')


