import json
import math
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import random
from transformers import BertTokenizer
from transformers import BertModel
from collections import Counter, defaultdict
from torch.utils.data import Dataset, DataLoader
import torch.optim as optim
import time
import copy
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from dataset_loader import load_data

random.seed(42)
evidence_key_prefix = 'evidence-'
er_filename = "../project-data/evidence-retrival-only-results.json"

# ----------Hyperparameters of the entire pipeline----------
# --------------Evidence Retrival--------------
d_bert_base = 768
d_bert_large = 1024
gpu = 0
input_seq_max_len = 384
train_sample_ratio = 1
pre_select_evidence_num = 1000
loader_batch_size = 24
loader_worker_num = 2
num_epoch = 1
evidence_selection_threshold = 0.7
hnm_threshold = 0.01
max_evi = 5
# ----------------------------------------------


class CFEVERERTrainDataset(Dataset):
    """Climate Fact Extraction and Verification Dataset for Train, for the Evidence Retrival task."""

    def __init__(self, claims, evidences_, max_len=input_seq_max_len, sample_ratio=train_sample_ratio):
        self.data_set = unroll_train_claim_evidences(claims, evidences_, sample_ratio=sample_ratio)
        self.max_len = max_len
        self.claims = claims
        self.evidences = evidences_
        self.sample_ratio = sample_ratio

        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

    def __len__(self):
        return len(self.data_set)
    
    def reset_data(self, claim_hard_negatives_pos):
        #self.data_set = unroll_claim_evidences(self.claims, self.evidences, self.is_train, train_sample_ratio, pre_select_evidence_num)
        self.data_set = handle_reset_train_data(claim_hard_negatives_pos, self.evidences, self.sample_ratio)

    def __getitem__(self, index):
        claim_id, evidence_id, label = self.data_set[index]

        # Preprocessing the text to be suitable for BERT
        claim_evidence_in_tokens = self.tokenizer.encode_plus(self.claims[claim_id]['claim_text'], self.evidences[evidence_id], 
                                                              return_tensors='pt', padding='max_length', truncation=True,
                                                              max_length=self.max_len, return_token_type_ids=True)
        
        seq, attn_masks, segment_ids, position_ids = claim_evidence_in_tokens['input_ids'].squeeze(0), claim_evidence_in_tokens[
                'attention_mask'].squeeze(0), claim_evidence_in_tokens['token_type_ids'].squeeze(0), torch.tensor([i+1 for i in range(self.max_len)])
    
        return seq, attn_masks, segment_ids, position_ids, label, claim_id, evidence_id


class CFEVERERTestDataset(Dataset):
    """Climate Fact Extraction and Verification Dataset for Dev/Test, for the Evidence Retrival task."""

    def __init__(self, claims, evidences_, max_len=input_seq_max_len, max_candidates=pre_select_evidence_num):
        self.data_set = unroll_test_claim_evidences(claims, evidences_, max_candidates=max_candidates)
        self.max_len = max_len
        self.claims = claims
        self.evidences = evidences_

        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

    def __len__(self):
        return len(self.data_set)

    def __getitem__(self, index):
        claim_id, evidence_id = self.data_set[index]

        # Preprocessing the text to be suitable for BERT
        claim_evidence_in_tokens = self.tokenizer.encode_plus(self.claims[claim_id]['claim_text'], self.evidences[evidence_id], 
                                                              return_tensors='pt', padding='max_length', truncation=True,
                                                              max_length=self.max_len, return_token_type_ids=True)
        
        seq, attn_masks, segment_ids, position_ids = claim_evidence_in_tokens['input_ids'].squeeze(0), claim_evidence_in_tokens[
                'attention_mask'].squeeze(0), claim_evidence_in_tokens['token_type_ids'].squeeze(0), torch.tensor([i+1 for i in range(self.max_len)])
    
        return seq, attn_masks, segment_ids, position_ids, claim_id, evidence_id


def unroll_train_claim_evidences(claims, evidences_, sample_ratio):
    st = time.time()

    train_claim_evidence_pairs = []
    for claim in claims:
        for train_evidence_id, label in generate_train_evidence_samples(evidences_, claims[claim]['evidences'], sample_ratio):
            train_claim_evidence_pairs.append((claim, train_evidence_id, label))

    random.shuffle(train_claim_evidence_pairs)
    print(f"Finished unrolling train claim-evidence pairs in {time.time() - st} seconds.")

    return train_claim_evidence_pairs


def unroll_test_claim_evidences(claims, evidences_, max_candidates):
    st = time.time()

    vectorizer = TfidfVectorizer(stop_words='english')
    vectorizer.fit(list(evidences.values()) + [claims[c]["claim_text"] for c in claims])
    evidences_tfidf = vectorizer.transform(evidences.values())

    test_claim_evidence_pairs = []
    for claim in claims:
        claim_tfidf = vectorizer.transform([claims[claim]['claim_text']])

        for test_evidence_id in generate_test_evidence_candidates(evidences_, evidences_tfidf, claim_tfidf, max_candidates):
            test_claim_evidence_pairs.append((claim, test_evidence_id))

    print(f"Finished unrolling test claim-evidence pairs in {time.time() - st} seconds.")

    return test_claim_evidence_pairs


def handle_reset_train_data(claim_hard_negatives_pos, evidences_, sample_ratio):
    train_claim_evidence_pairs = []
    for claim in claim_hard_negatives_pos:
        pos_count = 0
        for train_evidence_id, label in claim_hard_negatives_pos[claim]:
            train_claim_evidence_pairs.append((claim, train_evidence_id, label))

            if label == 1:
                pos_count += 1

        supplement_num = pos_count * (sample_ratio + 1) + math.floor(pos_count/2) * sample_ratio - len(claim_hard_negatives_pos[claim])
        generated = []

        for i in range(supplement_num):
            neg_sample = evidence_key_prefix + str(random.randint(0, len(evidences_) - 1))

            if neg_sample not in claim_hard_negatives_pos[claim] + generated:
                train_claim_evidence_pairs.append((claim, neg_sample, 0))
                generated.append(neg_sample)

    random.shuffle(train_claim_evidence_pairs)
    
    return train_claim_evidence_pairs


def generate_train_evidence_samples(evidences_, claim_evidences, sample_ratio):
    """
    Generate training samples for each of the claims for the evidence retrieval task.
    :param evidences_: the full evidence set.
    :param claim_evidences: the ground truth evidence set for the claim. In the form of a list of evidence ids
    :param sample_ratio: the ratio of positive to negative samples: neg/pos
    :return: a list of evidence samples zipped with the corresponding labels. - (evi id, label)
    """
        
    # Get positive samples
    samples = claim_evidences.copy()  # evidence ids

    # Get negative samples
    while len(samples) < len(claim_evidences) * (sample_ratio + 1):
        neg_sample = evidence_key_prefix + str(random.randint(0, len(evidences_) - 1))  # random selection
        
        if neg_sample not in samples:
            samples.append(neg_sample)

    samples_with_labels = list(zip(samples, [1] * len(claim_evidences) + [0] * (len(samples) - len(claim_evidences))))

    return samples_with_labels


def generate_test_evidence_candidates(evidences_, evidences_tfidf, claim_tfidf, max_candidates):
    """
    :param evidences_: the full evidence set.
    :param evidences_tfidf: The tfidf matrix of the entire evidence set
    :param claim_tfidf: The tfidf vector of the query claim (also a matrix technically).
    :param max_candidates: Number of evidences to be selected for further processing.
    :return: a list of the selected evidences.
    """
    similarity = cosine_similarity(claim_tfidf, evidences_tfidf).squeeze()
    
    df = pd.DataFrame({"evidences": evidences_.keys(), "similarity": similarity}).sort_values(by=['similarity'], ascending=False)
    potential_relevant_evidences = df.iloc[:max_candidates]["evidences"].tolist()

    return potential_relevant_evidences


class CFEVERERClassifier(nn.Module):

    def __init__(self):
        super(CFEVERERClassifier, self).__init__()

        # Instantiating BERT model object
        self.bert = BertModel.from_pretrained('bert-base-uncased')

        # Classification layer
        # input dimension is 768 because [CLS] embedding has a dimension of 768, if bert base is used
        # output dimension is 1 because we're working with a binary classification problem - RELEVANT : NOT RELEVANT
        self.cls_layer = nn.Linear(d_bert_base, 1)

    def forward(self, seq, attn_masks, segment_ids, position_ids):
        '''
        Inputs:
            -seq : Tensor of shape [B, T] containing token ids of sequences
            -attn_masks : Tensor of shape [B, T] containing attention masks to be used to avoid contibution of PAD tokens
            -segment_ids : Tensor of shape [B, T] containing token ids of segment embeddings (see BERT paper for more details)
            -position_ids : Tensor of shape [B, T] containing token ids of position embeddings (see BERT paper for more details)
        '''
        
        # Feeding the input to BERT model to obtain contextualized representations
        outputs = self.bert(seq, attention_mask=attn_masks, token_type_ids=segment_ids, position_ids=position_ids, return_dict=True)
        cont_reps = outputs.last_hidden_state

        # Obtaining the representation of [CLS] head (the first token)
        cls_rep = cont_reps[:, 0]

        # Feeding cls_rep to the classifier layer
        logits = self.cls_layer(cls_rep)

        return logits


def extract_hard_negatives(df, threshold=hnm_threshold):
    claim_hard_negatives_pos = defaultdict(list)

    pos_counts = df[df['labels'] == 1].groupby('claim_ids').size().reset_index(name='pos_count').set_index('claim_ids')['pos_count'].to_dict()
    df_true = df[df['labels'] == 1]

    df = df.groupby('claim_ids').apply(lambda x: x[(x['probs'] > threshold) & (x['labels'] == 0)].nlargest(pos_counts[x.name] * train_sample_ratio, 'probs')).reset_index(drop=True)  # find HNs
    df_concat = pd.concat([df_true, df]).reset_index(drop=True)

    for _, row in df_concat.iterrows():
        claim_id = row['claim_ids']
        evidence_id = row['evidence_ids']

        # A claim does not necessarily contain HNs
        claim_hard_negatives_pos[claim_id].append((evidence_id, row['labels']))
        
    return claim_hard_negatives_pos


def train_evi_retrival(net, loss_criterion, opti, train_loader, dev_loader, train_set, dev_claims, gpu, max_eps=num_epoch):
    best_f1 = 0
    
    for ep in range(max_eps):
        net.train()  # Good practice to set the mode of the model
        st = time.time()

        df = pd.DataFrame()
        
        for i, (seq, attn_masks, segment_ids, position_ids, labels, claim_ids, evidence_ids) in enumerate(train_loader):
            # Reset/Clear gradients
            opti.zero_grad()

            # Extracting the tokens ids, attention masks and token type ids
            seq, attn_masks, segment_ids, position_ids, labels = seq.cuda(gpu), attn_masks.cuda(gpu), segment_ids.cuda(gpu), position_ids.cuda(gpu), labels.cuda(gpu)

            # Obtaining the logits from the model
            logits = net(seq, attn_masks, segment_ids, position_ids)

            # Computing loss
            loss = loss_criterion(logits.squeeze(-1), labels.float())

            # Backpropagating the gradients, account for gradients
            loss.backward()

            # Optimization step, apply the gradients
            opti.step()

            df = pd.concat([df, pd.DataFrame({"claim_ids": claim_ids, "evidence_ids": evidence_ids, "labels": labels.detach().cpu(), "probs": get_probs_from_logits(logits).detach().cpu()})], ignore_index=True)

            if i % 100 == 0:
                acc = get_accuracy_from_logits(logits, labels)
                print("Iteration {} of epoch {} complete. Loss: {}; Accuracy: {}; Time taken (s): {}".format(i, ep, loss.item(), acc, (time.time() - st)))
                st = time.time()
        
        st = time.time()
        print("\nReseting training data...")
        train_set.reset_data(extract_hard_negatives(df))
        train_loader = DataLoader(train_set, batch_size=loader_batch_size, num_workers=loader_worker_num)
        print(f"Training data reset! Time taken: {time.time() - st}.\n")
        
        f1, recall, precision = evaluate(net, dev_loader, dev_claims, gpu)
        print("\nEpoch {} complete! Development F1: {}; Development Recall: {}; Development Precision: {}".format(ep, f1, recall, precision))
        if f1 > best_f1:
            print("Best development f1 improved from {} to {}, saving model...\n".format(best_f1, f1))
            best_f1 = f1
            torch.save(net.state_dict(), '/content/drive/MyDrive/Colab Notebooks/Assignment3/cfeverercls.dat')


def get_accuracy_from_logits(logits, labels):
    probs = torch.sigmoid(logits.unsqueeze(-1))
    preds = (probs > 0.5).long()
    acc = (preds.squeeze() == labels).float().mean()
    return acc


def get_probs_from_logits(logits):
    probs = torch.sigmoid(logits.unsqueeze(-1))

    return probs.squeeze()


def select_evi_candidates_df(df, threshold, max_candidates):
    max_prob_evi = df[df['probs'] == df['probs'].max()]

    df = df[df['probs'] > threshold].nlargest(max_candidates, "probs")

    if len(df) == 0:
        df = max_prob_evi

    return df

def predict(net, dataloader, gpu, threshold=evidence_selection_threshold, max_candidates=max_evi):
    net.eval()

    claim_evidences = defaultdict(list)
    df = pd.DataFrame()

    with torch.no_grad():  # suspend grad track, save time and memory
        for seq, attn_masks, segment_ids, position_ids, claim_ids, evidence_ids in dataloader:
            seq, attn_masks, segment_ids, position_ids = seq.cuda(gpu), attn_masks.cuda(gpu), segment_ids.cuda(gpu), position_ids.cuda(gpu)
            logits = net(seq, attn_masks, segment_ids, position_ids)
            probs = get_probs_from_logits(logits)
            
            df = pd.concat([df, pd.DataFrame({"claim_ids": claim_ids, "evidence_ids": evidence_ids, "probs": probs.cpu()})], ignore_index=True)

    # groupby gives a df for each claim_ids, then for each df, apply() the selection, finally reset_index to get rid of the multi-index
    filtered_claim_evidences_df = df.groupby('claim_ids').apply(lambda x: select_evi_candidates_df(x, threshold, max_candidates)).reset_index(drop=True)

    for _, row in filtered_claim_evidences_df.iterrows():
        claim_id = row['claim_ids']
        evidence_id = row['evidence_ids']

        claim_evidences[claim_id].append(evidence_id)
    
    return claim_evidences


def extract_er_result(claim_evidences, claims, filename=er_filename):
    extracted_claims = copy.deepcopy(claims)

    for c in extracted_claims:
        extracted_claims[c]["evidences"] = claim_evidences[c]
    
    with open(filename, 'w') as f:
        json.dump(extracted_claims, f)

    return extracted_claims


def evaluate(net, dataloader, dev_claims, gpu):
    claim_evidences = predict(net, dataloader, gpu)
    extract_er_result(claim_evidences, dev_claims)

    fscores, recalls, precisions = [], [], []

    for claim_id, evidences in claim_evidences.items():
        e_true = dev_claims[claim_id]['evidences']
        recall = len([e for e in evidences if e in e_true]) / len(e_true)
        precision = len([e for e in evidences if e in e_true]) / len(evidences)
        fscore = 2 * (precision * recall) / (precision + recall) if precision + recall != 0 else 0.0

        fscores.append(fscore)
        precisions.append(precision)
        recalls.append(recall)

    mean_f = np.mean(fscores if len(fscores) > 0 else [0.0])
    mean_recall = np.mean(recalls if len(recalls) > 0 else [0.0])
    mean_precision = np.mean(precisions if len(precisions) > 0 else [0.0])

    return mean_f, mean_recall, mean_precision  # F1 Score, recall, precision


def test_h(train_claims, dev_claims, evidences):
    # print([evidences[e] for e in train_claims['claim-169']['evidences']])
    train_claims = {**dict(train_claims), **dict(dev_claims)}

    label_counter = Counter()
    evidence_num_counter = Counter()
    evidence_len_counter = Counter()
    biggest_claim = (0, "")
    biggest = (0, "", "")
    count = 0
    for claim in train_claims:
        biggest_claim = max([biggest_claim, (len(train_claims[claim]['claim_text'].split()), train_claims[claim]['claim_text'])],
                            key=lambda x: x[0])
        label_counter.update([train_claims[claim]['claim_label']])
        evidence_num_counter.update([len(train_claims[claim]['evidences'])])
        for e in train_claims[claim]['evidences']:
            biggest = max([biggest, (len(evidences[e].split()), evidences[e], train_claims[claim]['claim_text'])],
                          key=lambda x: x[0])

        for i, e in enumerate(train_claims[claim]['evidences']):
            evidence_len_counter.update([len(evidences[e].split())])

        if train_claims[claim]['claim_label'] == 'REFUTES':
            print("------------------------------")
            print(f"Claim Label: {train_claims[claim]['claim_label']}")
            print(f"Claim text: {train_claims[claim]['claim_text']}")
            for i, e in enumerate(train_claims[claim]['evidences']):
                print(i, evidences[e])
            print("------------------------------")
            print("\n\n")

    print(
        f'Label Counter: {label_counter}, Len Counter:{evidence_num_counter}, Count: {count}, total: {len(train_claims)}, percentage: {count / len(train_claims)}')
    print("\n\n")
    print("Biggest evidence: ", biggest)
    print("\n\n")
    print("Biggest claim: ", biggest_claim)


if __name__ == '__main__':
    random.seed(42)
    train_claims, dev_claims, test_claims, evidences = load_data()

    #-------------------------------------------------------------

    # # Creating instances of training, test and development set
    # train_set = CFEVERERTrainDataset(train_claims, evidences)
    # dev_set = CFEVERERTestDataset(dev_claims, evidences)
    # #test_set = CFEVERERDataset(test_claims, evidences)

    # #Creating intsances of training, test and development dataloaders
    # train_loader = DataLoader(train_set, batch_size=loader_batch_size, num_workers=loader_worker_num)
    # dev_loader = DataLoader(dev_set, batch_size=loader_batch_size, num_workers=loader_worker_num)
    # #test_loader = DataLoader(test_set, batch_size=loader_batch_size, num_workers=loader_worker_num)

    # net = CFEVERERClassifier()
    # #net.load_state_dict(torch.load('/content/drive/MyDrive/Colab Notebooks/Assignment3/cfeverercls.dat')
    # net.cuda(gpu) #Enable gpu support for the model

    # loss_criterion = nn.BCEWithLogitsLoss()
    # opti = optim.Adam(net.parameters(), lr=2e-5)

    # # fine-tune the model
    # train_evi_retrival(net, loss_criterion, opti, train_loader, dev_loader, train_set, dev_claims, gpu)

    # # claim_evidences = predict(net, test_loader, gpu)
    # # test_claims = extract_er_result(claim_evidences, test_claims)

    #-------------------------------------------------------------

    test_h(train_claims, dev_claims, evidences)