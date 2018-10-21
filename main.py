# -*- coding: utf-8 -*-
import os
cudaid = 0
os.environ["CUDA_VISIBLE_DEVICES"] = str(cudaid)

import sys
import time
import numpy as np
import cPickle as pickle
import copy
import random
from random import shuffle
import math

import torch
import torch.nn as nn
from torch.autograd import Variable

import data as datar
from model import *
from utils_pg import *
from configs import *

cfg = DeepmindConfigs()
TRAINING_DATASET_CLS = DeepmindTraining
TESTING_DATASET_CLS = DeepmindTesting

def print_basic_info(modules, consts, options):
    if options["is_debugging"]:
        print "\nWARNING: IN DEBUGGING MODE\n"
    
    if options["has_learnable_w2v"]:
        print "USE LEARNABLE W2V EMBEDDING"
    if options["is_bidirectional"]:
        print "USE BI-DIRECTIONAL RNN"
    if options["has_lvt_trick"]:
        print "USE LVT TRICK"
    if options["omit_eos"]:
        print "<eos> IS OMITTED IN TESTING DATA"
    if options["prediction_bytes_limitation"]:
        print "MAXIMUM BYTES IN PREDICTION IS LIMITED"
    for k in consts:
        print k + ":", consts[k]

def init_modules():
    
    init_seeds()

    options = {}

    options["is_debugging"] = False
    options["is_predicting"] = False

    options["cuda"] = cfg.CUDA and torch.cuda.is_available()
    options["device"] = torch.device("cuda" if  options["cuda"] else "cpu")

    options["cell"] = cfg.CELL
    options["copy"] = cfg.COPY
    options["coverage"] = cfg.COVERAGE

    assert TRAINING_DATASET_CLS.IS_UNICODE == TESTING_DATASET_CLS.IS_UNICODE
    options["is_unicode"] = TRAINING_DATASET_CLS.IS_UNICODE
    options["has_y"] = TRAINING_DATASET_CLS.HAS_Y
    
    options["has_lvt_trick"] = False
    options["has_learnable_w2v"] = True
    options["is_bidirectional"] = True
    options["beam_decoding"] = True # False for greedy decoding
    options["omit_eos"] = False # omit <eos> and continuously decode until length of sentence reaches MAX_LEN_PREDICT (for DUC testing data)
    options["prediction_bytes_limitation"] = False if TESTING_DATASET_CLS.MAX_BYTE_PREDICT == None else True

    assert options["is_unicode"] == False

    consts = {}

    consts["idx_gpu"] = cudaid

    consts["dim_x"] = cfg.DIM_X
    consts["dim_y"] = cfg.DIM_Y
    consts["len_x"] = cfg.MAX_LEN_X + 1 # plus 1 for eos
    consts["len_y"] = cfg.MAX_LEN_Y + 1
    consts["num_x"] = cfg.MAX_NUM_X
    consts["num_y"] = cfg.NUM_Y
    consts["hidden_size"] = cfg.HIDDEN_SIZE

    consts["lvt_dict_size"] = 200 if options["is_debugging"] else cfg.LVT_DICT_SIZE

    consts["batch_size"] = 5 if options["is_debugging"] else TRAINING_DATASET_CLS.BATCH_SIZE
    if options["is_debugging"]:
        consts["testing_batch_size"] = 1 if options["beam_decoding"] else 2
    else:
        #consts["testing_batch_size"] = 1 if options["beam_decoding"] else TESTING_DATASET_CLS.BATCH_SIZE 
        consts["testing_batch_size"] = TESTING_DATASET_CLS.BATCH_SIZE

    consts["min_len_predict"] = TESTING_DATASET_CLS.MIN_LEN_PREDICT
    consts["max_len_predict"] = TESTING_DATASET_CLS.MAX_LEN_PREDICT
    consts["max_byte_predict"] = TESTING_DATASET_CLS.MAX_BYTE_PREDICT
    consts["testing_print_size"] = TESTING_DATASET_CLS.PRINT_SIZE

    consts["top_k"] = 1
    consts["lr"] = 0.15
    consts["beam_size"] = 4

    consts["max_epoch"] = 300 if options["is_debugging"] else 30 
    consts["num_model"] = 1
    consts["print_time"] = 5
    consts["save_epoch"] = 1

    assert consts["dim_x"] == consts["dim_y"]
    assert consts["top_k"] <= cfg.MIN_NUM_X
    assert consts["beam_size"] >= 1
    if options["has_lvt_trick"]:
        assert consts["lvt_dict_size"] != None
        assert consts["testing_batch_size"] <= consts["batch_size"]
        assert consts["lvt_dict_size"] <= cfg.NUM_FREQUENT_WORDS

    modules = {}
    
    [_, dic, hfw, w2i, i2w, w2w] = pickle.load(open(cfg.cc.TRAINING_DATA_PATH + "dic.pkl", "r")) 
    consts["dict_size"] = len(dic)
    modules["dic"] = dic
    modules["w2i"] = w2i
    modules["i2w"] = i2w
    if options["has_lvt_trick"]:
        modules["freq_words"] = hfw
    modules["lfw_emb"] = modules["w2i"][cfg.W_UNK]
    modules["eos_emb"] = modules["w2i"][cfg.W_EOS]
    consts["pad_token_idx"] = modules["w2i"][cfg.W_PAD]

    return modules, consts, options

def greedy_decode(flist, batch, model, modules, consts, options):
    testing_batch_size = len(flist)

    dec_result = [[] for i in xrange(testing_batch_size)]
    existence = [True] * testing_batch_size
    num_left = testing_batch_size

    word_emb, dec_state, x_mask, y, len_y = batch
    next_y = torch.LongTensor(np.ones((1, testing_batch_size), dtype="int64")).cuda()

    for step in xrange(consts["max_len_predict"]):
        if num_left == 0:
            break
        y_pred, dec_state = model.decode_once(next_y, word_emb, dec_state, x_mask)
        dict_size = y_pred.shape[-1]
        y_pred = y_pred.view(testing_batch_size, dict_size)
        dec_state = dec_state.view(testing_batch_size, dec_state.shape[-1])
        next_y = torch.argmax(y_pred, 1).view((1, testing_batch_size))

        for idx_doc in xrange(testing_batch_size):
            if existence[idx_doc] == False:
                continue

            idx_max = next_y[0, idx_doc].item()
            if options["has_lvt_trick"]:
                idx_max = lvt_i2i[idx_max]
                next_y[0, idx_doc] = idx_max
            if idx_max == modules["eos_emb"]:
                existence[idx_doc] = False
                num_left -= 1
            else:
                dec_result[idx_doc].append(str(idx_max))

    if options["prediction_bytes_limitation"]:
        for i in xrange(len(dec_result)):
            sample = dec_result[i]
            b = 0
            for j in xrange(len(sample)):
                b += len(sample[j])
                if b > consts["max_byte_predict"]:
                    dec_result[i] = dec_result[i][0 : j]
                    break

    for idx_doc in xrange(testing_batch_size):
        fname = str(flist[idx_doc])
        if len(dec_result[idx_doc]) >= consts["min_len_predict"]:
            write_summ("".join((cfg.cc.SUMM_PATH, fname)), dec_result[idx_doc], 1, options)
            write_summ("".join((cfg.cc.BEAM_SUMM_PATH, fname)), dec_result[idx_doc], 1, options, modules["i2w"])
            if options["has_y"]:
                ly = len_y[idx_doc]
                y_true = y[0 : ly, idx_doc].tolist()
                y_true = [str(i) for i in y_true[:-1]] # delete <eos>
                write_summ("".join((cfg.cc.GROUND_TRUTH_PATH, fname)), y_true, 1, options)
                write_summ("".join((cfg.cc.BEAM_GT_PATH, fname)), y_true, 1, options, modules["i2w"])

def beam_decode(fname, batch, model, modules, consts, options):
    fname = str(fname)

    beam_size = consts["beam_size"]
    num_live = 1
    num_dead = 0
    samples = []
    sample_scores = np.zeros(beam_size)

    last_traces = [[]]
    last_scores = torch.FloatTensor(np.zeros(1)).cuda()
    last_states = []

    x, word_emb, dec_state, x_mask, y, len_y, ref_sents = batch
    next_y = torch.LongTensor(-np.ones((1, num_live, 1), dtype="int64")).cuda()
    x = x.unsqueeze(1)
    word_emb = word_emb.unsqueeze(1)
    x_mask = x_mask.unsqueeze(1)
    dec_state = dec_state.unsqueeze(0)
    if options["cell"] == "lstm":
        dec_state = (dec_state, dec_state)
    
    for step in xrange(consts["max_len_predict"]):
        tile_word_emb = word_emb.repeat(1, num_live, 1)
        tile_x_mask = x_mask.repeat(1, num_live, 1)
        tile_x = x.repeat(1, num_live)

        y_pred, dec_state = model.decode_once(tile_x, next_y, tile_word_emb, dec_state, tile_x_mask)
        dict_size = y_pred.shape[-1]
        y_pred = y_pred.view(num_live, dict_size)
    
        if options["cell"] == "lstm":
            dec_state = (dec_state[0].view(num_live, dec_state[0].shape[-1]), dec_state[1].view(num_live, dec_state[1].shape[-1]))
        else:
            dec_state = dec_state.view(num_live, dec_state.shape[-1])
  
        cand_scores = last_scores + torch.log(y_pred) # 分数最大越好
        cand_scores = cand_scores.flatten()
        idx_top_joint_scores = torch.topk(cand_scores, beam_size - num_dead)[1]


        idx_last_traces = idx_top_joint_scores / dict_size
        idx_word_now = idx_top_joint_scores % dict_size
        top_joint_scores = cand_scores[idx_top_joint_scores]

        traces_now = []
        scores_now = np.zeros((beam_size - num_dead))
        states_now = []
        
        for i, [j, k] in enumerate(zip(idx_last_traces, idx_word_now)):
            if options["has_lvt_trick"]:
                traces_now.append(last_traces[j] + [batch.lvt_i2i[k]])
            else:
                traces_now.append(last_traces[j] + [k])
            scores_now[i] = copy.copy(top_joint_scores[i])
            if options["cell"] == "lstm":
                states_now.append((copy.copy(dec_state[0][j, :]), copy.copy(dec_state[1][j, :])))
            else:
                states_now.append(copy.copy(dec_state[j, :]))


        num_live = 0
        last_traces = []
        last_scores = []
        last_states = []

        for i in xrange(len(traces_now)):
            if traces_now[i][-1] == modules["eos_emb"] and len(traces_now[i]) >= consts["min_len_predict"]:
                samples.append([str(e.item()) for e in traces_now[i][:-1]])
                sample_scores[num_dead] = scores_now[i]
                num_dead += 1
            else:
                last_traces.append(traces_now[i])
                last_scores.append(scores_now[i])
                last_states.append(states_now[i])
                num_live += 1
        if num_live == 0 or num_dead >= beam_size:
            break

        last_scores = torch.FloatTensor(np.array(last_scores).reshape((num_live, 1))).cuda()
        next_y = np.array([e[-1] for e in last_traces], dtype = "int64").reshape((1, num_live))
        next_y = torch.LongTensor(next_y).cuda()
        if options["cell"] == "lstm":
            h_states = []
            c_states = []
            for state in last_states:
                h_states.append(state[0])
                c_states.append(state[1])
            dec_state = (torch.stack(h_states).view((num_live, h_states[0].shape[-1])),\
                         torch.stack(c_states).view((num_live, c_states[0].shape[-1])))
        else:
            dec_state = torch.stack(last_states).view((num_live, dec_state.shape[-1]))
        assert num_live + num_dead == beam_size

    if num_live > 0:
        for i in xrange(num_live):
            samples.append([str(e.item()) for e in last_traces[i]])
            sample_scores[num_dead] = last_scores[i]
            num_dead += 1
    
    #weight by length
    for i in xrange(len(sample_scores)):
        sent_len = float(len(samples[i]))
        sample_scores[i] = sample_scores[i] #*  math.exp(-sent_len / 10)

    idx_sorted_scores = np.argsort(sample_scores) # 低分到高分
    if options["has_y"]:
        ly = len_y[0]
        y_true = y[0 : ly].tolist()
        y_true = [str(i) for i in y_true[:-1]] # delete <eos>

    sorted_samples = []
    sorted_scores = []
    filter_idx = []
    for e in idx_sorted_scores:
        if len(samples[e]) >= consts["min_len_predict"]:
            filter_idx.append(e)
    if len(filter_idx) == 0:
        filter_idx = idx_sorted_scores
    for e in filter_idx:
        sorted_samples.append(samples[e])
        sorted_scores.append(sample_scores[e])

    num_samples = len(sorted_samples)
    if len(sorted_samples) == 1:
        sorted_samples = sorted_samples[0]
        num_samples = 1

    if options["prediction_bytes_limitation"]:
        for i in xrange(len(sorted_samples)):
            sample = sorted_samples[i]
            b = 0
            for j in xrange(len(sample)):
                b += len(sample[j])
                if b > consts["max_byte_predict"]:
                    sorted_samples[i] = sorted_samples[i][0 : j]
                    break

    dec_words = [modules["i2w"][int(e)] for e in sorted_samples[-1]]
    # for rouge
    write_for_rouge(fname, ref_sents, dec_words, cfg)

    # beam search history
    write_summ("".join((cfg.cc.BEAM_SUMM_PATH, fname)), sorted_samples, num_samples, options, modules["i2w"], sorted_scores)
    write_summ("".join((cfg.cc.BEAM_GT_PATH, fname)), y_true, 1, options, modules["i2w"])
        #print "================="


def predict(model, modules, consts, options):
    print "start predicting,"
    options["has_y"] = TESTING_DATASET_CLS.HAS_Y
    if options["beam_decoding"]:
        print "using beam search"
    else:
        print "using greedy search"
    rebuild_dir(cfg.cc.BEAM_SUMM_PATH)
    rebuild_dir(cfg.cc.BEAM_GT_PATH)
    rebuild_dir(cfg.cc.GROUND_TRUTH_PATH)
    rebuild_dir(cfg.cc.SUMM_PATH)

    print "loading test set..."
    xy_list = pickle.load(open(cfg.cc.VALIDATE_DATA_PATH + "valid.pkl", "r")) 
    batch_list, num_files, num_batches = datar.batched(len(xy_list), options, consts)

    print "num_files = ", num_files, ", num_batches = ", num_batches
    
    running_start = time.time()
    partial_num = 0
    total_num = 0
    si = 0
    for idx_batch in xrange(num_batches):
        test_idx = batch_list[idx_batch]
        batch_raw = [xy_list[xy_idx] for xy_idx in test_idx]
        batch = datar.get_data(batch_raw, modules, consts, options)

        x, len_x, x_mask, y, len_y, y_mask, oy = sort_samples(batch.x, batch.len_x, batch.x_mask, batch.y, batch.len_y, batch.y_mask, batch.original_summarys)
        word_emb, dec_state = model.encode(torch.LongTensor(x).cuda(), torch.LongTensor(len_x).cuda(), torch.FloatTensor(x_mask).cuda())

        if options["beam_decoding"]:
            for idx_s in xrange(word_emb.size(1)):
                inputx = (torch.LongTensor(x[:, idx_s]).cuda(), word_emb[:, idx_s, :], dec_state[idx_s, :],\
                          torch.FloatTensor(x_mask[:, idx_s, :]).cuda(), y[:, idx_s], [len_y[idx_s]], oy[idx_s])
                beam_decode(si, inputx, model, modules, consts, options)
                si += 1
        else:
            inputx = (word_emb, dec_state, torch.FloatTensor(x_mask).cuda(), y, len_y)
            greedy_decode(test_idx, inputx, model, modules, consts, options)

        testing_batch_size = len(test_idx)
        partial_num += testing_batch_size
        total_num += testing_batch_size
        if partial_num >= consts["testing_print_size"]:
            print total_num, "summs are generated"
            partial_num = 0
    print si, total_num

def run(existing_model_name = None):
    modules, consts, options = init_modules()

    #use_gpu(consts["idx_gpu"])
    if options["is_predicting"]:
        need_load_model = True
        training_model = False
        predict_model = True
    else:
        need_load_model = False
        training_model = True
        predict_model = False

    print_basic_info(modules, consts, options)

    if training_model:
        print "loading train set..."
        if options["is_debugging"]:
            xy_list = pickle.load(open(cfg.cc.VALIDATE_DATA_PATH + "valid.pkl", "r")) 
        else:
            xy_list = pickle.load(open(cfg.cc.TRAINING_DATA_PATH + "train.pkl", "r")) 
        batch_list, num_files, num_batches = datar.batched(len(xy_list), options, consts)
        print "num_files = ", num_files, ", num_batches = ", num_batches

    running_start = time.time()
    if True: #TODO: refactor
        print "compiling model ..." 
        model = Model(modules, consts, options)
        criterion = nn.NLLLoss(ignore_index=consts["pad_token_idx"])
        if options["cuda"]:
            model.cuda()
            criterion.cuda()
            #model = nn.DataParallel(model)
        #optimizer = torch.optim.Adadelta(model.parameters(), lr=consts["lr"], rho=0.95)
        optimizer = torch.optim.Adagrad(model.parameters(), lr=consts["lr"], initial_accumulator_value=0.1)
        #optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        
        model_name = "cnndm.s2s"
        existing_epoch = 0
        if need_load_model:
            if existing_model_name == None:
                existing_model_name = "cnndm.s2s.gpu5.epoch14.1"
            print "loading existed model:", existing_model_name
            model, optimizer = load_model(cfg.cc.MODEL_PATH + existing_model_name, model, optimizer)

        if training_model:
            print "start training model "
            print_size = num_files / consts["print_time"] if num_files >= consts["print_time"] else num_files

            last_total_error = float("inf")
            print "max epoch:", consts["max_epoch"]
            for epoch in xrange(0, consts["max_epoch"]):
                '''
                if not options["is_debugging"] and epoch == 5:
                    consts["lr"] *= 0.1
                    #adjust
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = consts["lr"]
                '''
                print "epoch: ", epoch + existing_epoch
                num_partial = 1
                total_error = 0.0
                partial_num_files = 0
                epoch_start = time.time()
                partial_start = time.time()
                # shuffle the trainset
                batch_list, num_files, num_batches = datar.batched(len(xy_list), options, consts)
                used_batch = 0.
                for idx_batch in xrange(num_batches):
                    train_idx = batch_list[idx_batch]
                    batch_raw = [xy_list[xy_idx] for xy_idx in train_idx]
                    if len(batch_raw) != consts["batch_size"]:
                        continue
                    local_batch_size = len(batch_raw)
                    batch = datar.get_data(batch_raw, modules, consts, options)
                  
                    x, len_x, x_mask, y, len_y, y_mask, oy = sort_samples(batch.x, batch.len_x, \
                                                             batch.x_mask, batch.y, batch.len_y, batch.y_mask, batch.original_summarys)
                    
                    model.zero_grad()
                    y_pred, cost = model(torch.LongTensor(x).cuda(), torch.LongTensor(len_x).cuda(),\
                                   torch.LongTensor(y).cuda(), None, torch.FloatTensor(x_mask).cuda(), torch.FloatTensor(y_mask).cuda())
                    
                    cost.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
                    optimizer.step()
                    
                    cost = cost.item()
                    total_error += cost
                    used_batch += 1
                    partial_num_files += consts["batch_size"]
                    if partial_num_files / print_size == 1 and idx_batch < num_batches:
                        print idx_batch + 1, "/" , num_batches, "batches have been processed,", 
                        print "average cost until now:", "cost =", total_error / used_batch, ",", 
                        print "time:", time.time() - partial_start
                        partial_num_files = 0
                        if not options["is_debugging"]:
                            print "save model... ",
                            save_model(cfg.cc.MODEL_PATH + model_name + ".gpu" + str(consts["idx_gpu"]) + ".epoch" + str(epoch / consts["save_epoch"] + existing_epoch) + "." + str(num_partial), model, optimizer)
                            print "finished"
                        num_partial += 1
                print "in this epoch, total average cost =", total_error / used_batch, ",", 
                print "time:", time.time() - epoch_start

                if options["has_lvt_trick"]:
                    print_sent_dec(y_pred, batch.y, batch.y_mask, modules, consts, options, local_batch_size, batch.lvt_dict)
                else:
                    print_sent_dec(y_pred, y, y_mask, modules, consts, options, local_batch_size)
                
                if last_total_error > total_error or options["is_debugging"]:
                    last_total_error = total_error
                    if not options["is_debugging"]:
                        print "save model... ",
                        save_model(cfg.cc.MODEL_PATH + model_name + ".gpu" + str(consts["idx_gpu"]) + ".epoch" + str(epoch / consts["save_epoch"] + existing_epoch) + "." + str(num_partial), model, optimizer)
                        print "finished"
                else:
                    print "optimization finished"
                    break

            print "save final model... ",
            save_model(cfg.cc.MODEL_PATH + model_name + "final.gpu" + str(consts["idx_gpu"]) + ".epoch" + str(epoch / consts["save_epoch"] + existing_epoch) + "." + str(num_partial), model, optimizer)
            print "finished"
        else:
            print "skip training model"

        if predict_model:
            predict(model, modules, consts, options)
    print "Finished, time:", time.time() - running_start

if __name__ == "__main__":
    np.set_printoptions(threshold = np.inf)
    existing_model_name = sys.argv[1] if len(sys.argv) > 1 else None
    run(existing_model_name)
