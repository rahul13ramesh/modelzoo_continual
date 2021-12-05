import torch
import pickle
import numpy as np
import torchvision.transforms as transforms
from PIL import Image
from copy import deepcopy
from torch.utils.data import Dataset
from datasets.data import MultiTaskDataHandler, wif


class CustomTensorDataset(Dataset):
    """TensorDataset with support of transforms.
    """
    def __init__(self, tensors, transform=None):
        assert all(tensors[0].size(0) == tensor.size(0) for tensor in tensors)
        self.tensors = tensors
        self.transform = transform

    def __getitem__(self, index):
        x = self.tensors[0][index].numpy().astype(np.uint8)
        x = Image.fromarray(x)

        if self.transform:
            x = self.transform(x)
        y = list(self.tensors[1][index].numpy().astype(np.uint8))

        return x, y

    def __len__(self):
        return self.tensors[0].size(0)


class MiniImagenetHandler(MultiTaskDataHandler):
    def __init__(self, args, tasks):
        """
        Import mini-imagenet dataset and split it into multiple tasks with
        disjoint set of classes Implementation is based on the one provided by:
            Aljundi, Rahaf, et al. "Online continual learning with maximally
            interfered retrieval." arXiv preprint arXiv:1908.04742 (2019).
        :param args: Arguments for model/data configuration
        :return: Train, test and validation data loaders
        """
        self.tasks = tasks

        # Merge meta-train/meta-val and meta-test sets
        for i in ['train', 'val', 'test']:
            fname = "./data/mini_imagenet/mini-imagenet-cache-" + i + ".pkl"
            with open(fname, "rb") as fp:
                file_data = pickle.load(fp)
                data = file_data["image_data"]
            if i == 'train':
                main_data = data.reshape([64, 600, 84, 84, 3])
            else:
                app_data = data.reshape([(20 if i == 'test' else 16),
                                         600, 84, 84, 3])
                main_data = np.append(main_data, app_data, axis=0)

        all_data = main_data.reshape((60000, 84, 84, 3))
        all_label = np.array([[i] * 600 for i in range(100)]).flatten()

        train_ds, test_ds = [], []
        current_train, current_test = None, None

        cat = lambda x, y: np.concatenate((x, y), axis=0)

        # Split dataset into multiple tasks
        for task_id, task in tasks:
            current_train, current_test = None, None
            for label in task:
                class_indices = np.argwhere(all_label == label).reshape(-1)

                class_data = all_data[class_indices]
                class_label = all_label[class_indices]
                split = int(0.8 * class_data.shape[0])

                if task_id != len(tasks) - 1 and args.replay_frac < 0.99:
                    samples = int(args.replay_frac * split)
                    copies = 1 / (args.replay_frac * (len(tasks) - 1))
                    copies = max(int(copies), 1)

                    data_train = class_data[:samples]
                    label_train = class_label[:samples]

                    data_train = np.repeat(data_train, copies, axis=0)
                    label_train = np.repeat(label_train, copies, axis=0)
                else:
                    data_train = class_data[:split]
                    label_train = class_label[:split]

                data_test = class_data[split:]
                label_test = class_label[split:]

                if current_train is None:
                    current_train = (data_train, label_train)
                    current_test = (data_test, label_test)
                else:
                    current_train = (cat(current_train[0], data_train),
                                     cat(current_train[1], label_train))
                    current_test = (cat(current_test[0], data_test),
                                    cat(current_test[1], label_test))

            train_ds += [current_train]
            test_ds += [current_test]

        # Change labels to have label and task-id
        train_labels, test_labels = [], []
        train_data, test_data = []

        tmap = {}
        for task in tasks:
            for tid, t_orig in enumerate(task):
                tmap[t_orig] = tid

        for task_id, task_data in enumerate(train_ds):
            dataset, lab = task_data
            train_data.append(torch.Tensor(dataset))
            lab = [(task_id, tmap[li]) for li in lab]
            train_labels.append(lab)

        for task_id, task_data in enumerate(test_ds):
            dataset, lab = task
            test_data.append(torch.Tensor(dataset))
            lab = [(task_id, tmap[li]) for li in lab]
            test_labels.append(lab)

        train_data = torch.cat(train_data)
        train_labels = torch.Tensor(np.concatenate(train_labels))

        test_data = torch.cat(test_data)
        test_labels = torch.Tensor(np.concatenate(test_labels))

        # Convert to pytorch dataset
        mean_norm = [0.50, 0.50, 0.50]
        std_norm = [0.25, 0.25, 0.25]
        self.train_transform = transforms.Compose([
            transforms.RandomCrop(84, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(mean_norm, std_norm),
        ])
        self.vanilla_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=mean_norm, std=std_norm)])

        if args.epochs == 1:
            self.train_transform = self.vanilla_transform

        self.trainset = CustomTensorDataset(
            (train_data, train_labels), self.train_transform)
        self.testset = CustomTensorDataset(
            (test_data, test_labels), self.vanilla_transform)

    def get_task_data_loader(self, task, batch_size, workers, train=False):
        """
        Testloader for a specific task
        """
        if train:
            task_set = deepcopy(self.trainset)
        else:
            task_set = deepcopy(self.testset)

        task_ind = [task == i[0] for i in task_set.tensors[1]]

        inp = (task_set.tensors[0])[task_ind]
        labels = (task_set.tensors[1])[task_ind]
        dataset = CustomTensorDataset((inp, labels), self.vanilla_transform)

        loader = torch.utils.data.DataLoader(
            dataset, batch_size=batch_size,
            shuffle=False, num_workers=workers, pin_memory=True,
            worker_init_fn=wif)

        return loader
