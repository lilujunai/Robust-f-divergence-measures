import numpy as np
import csv
import torch.nn.parallel
import torch.optim
import torch.utils.data
import pickle
from tqdm import tqdm
import torch.nn as nn
from model import *
from noise_data_cifar_10_bias import *
import torch
import torchvision
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import init
from torch.autograd import Variable
import argparse
torch.autograd.set_detect_anomaly(True)
import math
#torch.autograd.set_detect_anomaly(True)
num_classes = 10
num_epochs = 100

CUDA = True if torch.cuda.is_available() else False
if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

Tensor = torch.cuda.FloatTensor if CUDA else torch.FloatTensor


#CE = nn.CrossEntropyLoss().cuda()

opt = parser.parse_args()

# Stable CE
class CrossEntropyLossStable(nn.Module):
    def __init__(self, reduction='mean', eps=1e-5):
        super(CrossEntropyLossStable, self).__init__()
        self._name = "Stable Cross Entropy Loss"
        self._eps = eps
        self._softmax = nn.Softmax(dim=-1)
        self._nllloss = nn.NLLLoss(reduction=reduction)

    def forward(self, outputs, labels):
        return self._nllloss( torch.log( self._softmax(outputs) + self._eps ), labels )

        
criterion = CrossEntropyLossStable()
criterion.cuda(device)

# Losses
div = opt.divergence

if div == 'KL':
    def activation(x): return -torch.mean(x)
    
    def conjugate(x): return -torch.mean(torch.exp(x - 1.))


elif div == 'Jeffrey':
    def activation(x): return -torch.mean(x)
    
    def conjugate(x): return -torch.mean(x + torch.mul(x, x) / 4. + torch.mul(torch.mul(x, x), x) / 16.)


elif div == 'Reverse-KL':
    def activation(x): return -torch.mean(-torch.exp(x))
    
    def conjugate(x): return -torch.mean(-1. - x)  # remove log
    
elif div == 'Squared-Hellinger':
    def activation(x): return -torch.mean(1. - torch.exp(x))
    def scale(nx): return nx
#    def conjugate(x): return -torch.mean(x / (1. - x))
    def conjugate(x): return -torch.mean((1. - torch.exp(x)) / (torch.exp(x)))

elif div == 'Pearson':
    def activation(x): return -torch.mean(x)
    def scale(nx): return nx
#    def conjugate(x): return -torch.mean(torch.mul(x, x) / 4. + x)
    def conjugate(x): return -torch.mean(torch.mul(x, x) / 4. + x)

elif div == 'Neyman':
    def activation(x): return -torch.mean(1. - torch.exp(x))

    def conjugate(x): return -torch.mean(2. - 2. * torch.sqrt(1. - x))

elif div == 'Jenson-Shannon':
    def activation(x): return -torch.mean(- torch.log(1. + torch.exp(-x))) - torch.log(torch.tensor(2.))

    def conjugate(x): return -torch.mean(x + torch.log(1. + torch.exp(-x))) + torch.log(torch.tensor(2.))

elif div == 'Total-Variation':
    def activation(x): return -torch.mean(torch.tanh(x) / 2.)
    
    def scale(nx):
#        nx -= nx.min()
#        nx /= nx.max()
        nx /= 2
#        nx *= (1-0.0005)
        return nx
    def conjugate(x): return -torch.mean(torch.tanh(x) / 2.)
#    def conjugate(x): return -torch.mean(x)
    
elif div == 'Peer-Loss':
    def activation(x): return x

    def conjugate(x): return x
    
else:
    raise NotImplementedError("[-] Not Implemented f-divergence %s" % div)

if args.scale == False:
    def scale(nx): return nx


# Stable PROB
class ProbLossStable(nn.Module):
    def __init__(self, reduction='none', eps=1e-5):
        super(ProbLossStable, self).__init__()
        self._name = "Prob Loss"
        self._eps = eps
        self._softmax = nn.Softmax(dim=-1)
        self._nllloss = nn.NLLLoss(reduction='none')

    def forward(self, outputs, labels):
#        return self._nllloss( scale(self._softmax(outputs)), labels )
        return self._nllloss( self._softmax(outputs), labels )
        
criterion_prob = ProbLossStable()
criterion_prob.cuda(device)

def train(train_loader, peer_loader, model, optimizer, epoch):

    model.train()
    peer_iter = iter(peer_loader)
    for i, (idx, input, target) in enumerate(train_loader):
        if idx.size(0) != batch_size:
            continue
        warmup_epoch = args.warmup
        input = torch.autograd.Variable(input.cuda())
        if args.r == 0.7 or args.r == 0.8:
            target = torch.Tensor(target[0].float())
        target = torch.autograd.Variable(target.cuda())
        output = model(input)
        optimizer.zero_grad()
        if epoch >= warmup_epoch:
            prob_reg = -criterion_prob(output, target.long())
            loss_regular = activation(prob_reg)
            for k in range(10):
                target_tmp = target * 0. + k
                if k == 9:
                    loss_regular -= transition_matrix[0][9] * activation(-criterion_prob(output, target_tmp.long()))
                else:
                    loss_regular -= transition_matrix[k+1][k] * activation(-criterion_prob(output, target_tmp.long()))
            input1 = peer_iter.next()[1]
            target2 = torch.randint(0, 10, (target.shape)).cuda()
            input1 = torch.autograd.Variable(input1.cuda())
            output1 = model(input1)
            prob_peer = -criterion_prob(output1, target2.long())
            loss_peer = conjugate(prob_peer)
            for k in range(10):
                target_tmp = target2 * 0. + k
                if k == 9:
                    loss_peer -= transition_matrix[0][9] * conjugate(-criterion_prob(output1, target_tmp.long()))
                else:
                    loss_peer -= transition_matrix[k+1][k] * conjugate(-criterion_prob(output1, target_tmp.long()))
            loss = loss_regular - loss_peer
#
        else:
            loss = criterion(output, target)
        loss.cuda(device)
        loss.backward()
        optimizer.step()


def test(model, test_loader, is_test = False):
    model.eval()
    correct = 0
    total = 0
    if is_test == True:
        for i, (idx, input, target) in enumerate(test_loader):
            input = torch.Tensor(input).cuda()
            if args.r == 0.7 or args.r == 0.8:
                target = torch.Tensor(target.float())
    
            target = torch.autograd.Variable(target).cuda()
            total += target.size(0)
            output = model(input)
            _, predicted = torch.max(output.detach(), 1)
            correct += predicted.eq(target.long()).sum().item()
    else:
        for i, (idx, input, target) in enumerate(test_loader):
            input = torch.Tensor(input).cuda()
            if args.r == 0.7 or args.r == 0.8:
                target = torch.Tensor(target[0].float())
            target = torch.autograd.Variable(target).cuda()
            total += target.size(0)
            output = model(input)
            _, predicted = torch.max(output.detach(), 1)
            correct += predicted.eq(target.long()).sum().item()
    
    accuracy = 100. * correct / total
    return accuracy

def f_calculate(model, data_loader, peer_loader):
    model.eval()
    f_score = 0
    peer_iter = iter(peer_loader)
    for i, (idx, input, target) in enumerate(data_loader):
        if idx.size(0) != batch_size:
            continue
        input = torch.autograd.Variable(input.cuda())
        if args.r == 0.7 or args.r == 0.8:
            target = torch.Tensor(target[0].float())
        target = torch.autograd.Variable(target.cuda())
        output = model(input)
        prob_reg = -criterion_prob(output.detach(), target.long())
        loss_regular = activation(prob_reg)
        for k in range(10):
            target_tmp = target * 0. + k
            if k == 9:
                loss_regular -= transition_matrix[0][9] * activation(-criterion_prob(output.detach(), target_tmp.long()))
            else:
                loss_regular -= transition_matrix[k+1][k] * activation(-criterion_prob(output.detach(), target_tmp.long()))
        
        
        input1 = peer_iter.next()[1]
        input1 = torch.autograd.Variable(input1.cuda())
        output1 = model(input1)
        target2 = torch.randint(0, 10, (target.shape)).cuda()
        prob_peer = -criterion_prob(output1.detach(), target2.long())
        loss_peer = conjugate(prob_peer)
        for k in range(10):
            target_tmp = target2 * 0. + k
            if k == 9:
                loss_peer -= transition_matrix[0][9] * conjugate(-criterion_prob(output1.detach(), target_tmp.long()))
            else:
                loss_peer -= transition_matrix[k+1][k] * conjugate(-criterion_prob(output1.detach(), target_tmp.long()))
        score = loss_peer - loss_regular
        f_score += score * target.size(0)
    return f_score/10000


def main(writer):
#    model_prob = resnet_cifar18_pre(num_classes=10).cuda()
    model_prob = torch.load('./trained_models/ce' + str(args.r) + '_' + str(args.s) + '_')
    best_prob_acc = 0
    max_f = -100
    val_acc_noisy_result = []
    val_acc_clean_result = []
    train_acc_result = []
    test_acc_result = []
    f_result = []
    peer_dataloader = peer_data(batch_size=args.batchsize, img_size=(32, 32))
    peer_val = peer_data_val(batch_size=args.batchsize, img_size=(32, 32))
    for epoch in range(num_epochs):
        print("epoch=", epoch,'r=', args.r)
        if epoch >= 0:
            learning_rate = 0.01
        if epoch >= 30:
            learning_rate = 0.001
        if epoch >= 60:
            learning_rate = 0.0001
        if epoch >= 90:
            learning_rate = 0.00001
        if epoch >= 120:
            learning_rate = 0.000001
        optimizer_prob = torch.optim.SGD(model_prob.parameters(), momentum=0.9, weight_decay=5e-4, lr=learning_rate)
        train(train_loader=train_loader_noisy, peer_loader = peer_dataloader, model=model_prob, optimizer=optimizer_prob, epoch=epoch)
        print("validating model_prob...")
        train_acc = test(model=model_prob, test_loader=train_loader_noisy, is_test = False)
        train_acc_result.append(train_acc)
        print('train_acc=', train_acc)
        valid_acc = test(model=model_prob, test_loader=valid_loader_noisy, is_test = False)
        val_acc_noisy_result.append(valid_acc)
        print('valid_acc_noise=', valid_acc)
        valid_acc2 = 0
        val_acc_clean_result.append(valid_acc2)
        print('valid_acc_clean=', valid_acc2)
        test_acc = test(model=model_prob, test_loader=test_loader_, is_test = True)
        test_acc_result.append(test_acc)
        print('test_acc=', test_acc)
        # gai
        f_div_value = f_calculate(model_prob, valid_loader_noisy, peer_val)
        f_result.append(f_div_value)
        print('f_div_value=', f_div_value)
        if epoch <= args.warmup:
            if valid_acc >= best_prob_acc:
                best_prob_acc = valid_acc
                torch.save(model_prob, './trained_models/Test_BIAS_load_lr6_' + str(args.r) + '_' + str(args.s) + '_' + str(args.divergence) + '_' + str(args.base)  + '_' + str(args.warmup)  + '_' + str(args.scale))
                print("saved, valid acc increases.")
        else:
            if f_div_value >= max_f:
                max_f = f_div_value
                torch.save(model_prob, './trained_models/Test_BIAS_load_lr6_' + str(args.r) + '_' + str(args.s) + '_' + str(args.divergence) + '_' + str(args.base)  + '_' + str(args.warmup)  + '_' + str(args.scale))
                print("saved, f-div value increases.")
        
        writer.writerow([epoch, train_acc, valid_acc, valid_acc2, test_acc, f_div_value])
    



def evaluate(path):
    model = torch.load(path)
    test_acc = test(model=model, test_loader=test_loader_)
    print('test_acc=', test_acc)



if __name__ == '__main__':
   
    print("Begin:")
    writer1 = csv.writer(open(f'Test_BIAS_load_lr6_result_{r}_{div}_{args.base}_{args.warmup}_scale{args.scale}.csv','w'))
    writer1.writerow(['Epoch', 'Training Acc', f'Val_Noisy_Acc', f'Val_clean_Acc', 'Test_ACC', 'f_div'])
    main(writer1)
    evaluate('./trained_models/Test_BIAS_load_lr6_' + str(args.r) + '_' + str(args.s) + '_' + str(args.divergence) + '_' + str(args.base)  + '_' + str(args.warmup)  + '_' + str(args.scale))
    print("Traning finished")