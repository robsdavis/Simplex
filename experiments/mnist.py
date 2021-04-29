import torch
import torchvision
import torch.optim as optim
import os
import seaborn as sns
import sklearn
import argparse
import pickle as pkl
import torch.nn.functional as F
from models.image_recognition import MnistClassifier
from explainers.simplex import Simplex
from explainers.nearest_neighbours import NearNeighLatent
from explainers.representer import Representer
from utils.schedulers import ExponentialScheduler
from visualization.images import plot_mnist
import matplotlib.pyplot as plt


# Load data
def load_mnist(batch_size: int, train: bool):
    return torch.utils.data.DataLoader(
        torchvision.datasets.MNIST('./data/', train=train, download=True,
                                   transform=torchvision.transforms.Compose([
                                       torchvision.transforms.ToTensor(),
                                       torchvision.transforms.Normalize(
                                           (0.1307,), (0.3081,))
                                   ])),
        batch_size=batch_size, shuffle=True)


def load_emnist(batch_size: int, train: bool):
    return torch.utils.data.DataLoader(
        torchvision.datasets.EMNIST('./data/', train=train, download=True, split='letters',
                                    transform=torchvision.transforms.Compose([
                                        torchvision.transforms.ToTensor(),
                                        torchvision.transforms.Normalize(
                                            (0.1307,), (0.3081,))
                                    ])),
        batch_size=batch_size, shuffle=True)


# Train model
def train_model(device: torch.device, n_epoch: int = 10, batch_size_train: int = 64, batch_size_test: int = 1000,
                random_seed: int = 42, learning_rate=0.01, momentum=0.5, log_interval=100, model_reg_factor=0.01,
                save_path='./results/mnist/', cv: int = 0):
    torch.random.manual_seed(random_seed + cv)
    torch.backends.cudnn.enabled = False

    # Prepare data
    train_loader = load_mnist(batch_size_train, train=True)
    test_loader = load_mnist(batch_size_test, train=False)

    # Create the model
    classifier = MnistClassifier()
    classifier.to(device)
    optimizer = optim.SGD(classifier.parameters(), lr=learning_rate, momentum=momentum, weight_decay=model_reg_factor)

    # Train the model
    train_losses = []
    train_counter = []
    test_losses = []

    def train(epoch):
        classifier.train()
        for batch_idx, (data, target) in enumerate(train_loader):
            data = data.to(device)
            target = target.to(device)
            optimizer.zero_grad()
            output = classifier(data)
            loss = F.nll_loss(output, target)
            loss.backward()
            optimizer.step()
            if batch_idx % log_interval == 0:
                print(f'Train Epoch: {epoch} [{batch_idx * len(data)}/{len(train_loader.dataset)}'
                      f' ({100. * batch_idx / len(train_loader):.0f}%)]\tLoss: {loss.item():.6f}')
                train_losses.append(loss.item())
                train_counter.append(
                    (batch_idx * 64) + ((epoch - 1) * len(train_loader.dataset)))
                torch.save(classifier.state_dict(), os.path.join(save_path, f'model_cv{cv}.pth'))
                torch.save(optimizer.state_dict(), os.path.join(save_path, f'optimizer_cv{cv}.pth'))

    def test():
        classifier.eval()
        test_loss = 0
        correct = 0
        with torch.no_grad():
            for data, target in test_loader:
                data = data.to(device)
                target = target.to(device)
                output = classifier(data)
                test_loss += F.nll_loss(output, target, reduction='sum').item()
                pred = output.data.max(1, keepdim=True)[1]
                correct += pred.eq(target.data.view_as(pred)).sum()
        test_loss /= len(test_loader.dataset)
        test_losses.append(test_loss)
        print(f'\nTest set: Avg. loss: {test_loss:.4f}, Accuracy: {correct}/{len(test_loader.dataset)}'
              f'({100. * correct / len(test_loader.dataset):.0f}%)\n')

    test()
    for epoch in range(1, n_epoch + 1):
        train(epoch)
        test()
    torch.save(classifier.state_dict(), os.path.join(save_path, f'model_cv{cv}.pth'))
    torch.save(optimizer.state_dict(), os.path.join(save_path, f'optimizer_cv{cv}.pth'))
    return classifier


# Train explainers
def fit_explainers(device: torch.device, explainers_name: list, corpus_size=1000, test_size=100,
                   n_epoch=10000, learning_rate=100.0, momentum=0.5, save_path='./results/mnist/',
                   random_seed: int = 42, n_keep=5, reg_factor_init=0.1, reg_factor_final=1000, cv: int = 0):
    torch.random.manual_seed(random_seed + cv)
    explainers = []

    # Load model:
    classifier = MnistClassifier()
    classifier.load_state_dict(torch.load(os.path.join(save_path, f'model_cv{cv}.pth')))
    classifier.to(device)
    classifier.eval()

    # Load data:
    corpus_loader = load_mnist(corpus_size, train=True)
    test_loader = load_mnist(test_size, train=False)
    corpus_examples = enumerate(corpus_loader)
    test_examples = enumerate(test_loader)
    batch_id_test, (test_data, test_targets) = next(test_examples)
    batch_id_corpus, (corpus_data, corpus_target) = next(corpus_examples)
    corpus_data = corpus_data.to(device).detach()
    test_data = test_data.to(device).detach()
    corpus_latent_reps = classifier.latent_representation(corpus_data).detach()
    corpus_probas = classifier.probabilities(corpus_data).detach()
    corpus_true_classes = torch.zeros(corpus_probas.shape, device=device)
    corpus_true_classes[torch.arange(corpus_size), corpus_target] = 1
    test_latent_reps = classifier.latent_representation(test_data).detach()

    # Fit corpus:
    reg_factor_scheduler = ExponentialScheduler(reg_factor_init, reg_factor_final, n_epoch)
    corpus = Simplex(corpus_examples=corpus_data,
                     corpus_latent_reps=corpus_latent_reps)
    weights = corpus.fit(test_examples=test_data,
                         test_latent_reps=test_latent_reps,
                         n_epoch=n_epoch, learning_rate=learning_rate, momentum=momentum,
                         reg_factor=reg_factor_init, n_keep=n_keep,
                         reg_factor_scheduler=reg_factor_scheduler)
    explainers.append(corpus)

    # Fit nearest neighbors:
    nn_uniform = NearNeighLatent(corpus_examples=corpus_data,
                                 corpus_latent_reps=corpus_latent_reps)
    nn_uniform.fit(test_examples=test_data,
                   test_latent_reps=test_latent_reps,
                   n_keep=n_keep)
    explainers.append(nn_uniform)
    nn_dist = NearNeighLatent(corpus_examples=corpus_data,
                              corpus_latent_reps=corpus_latent_reps,
                              weights_type='distance')
    nn_dist.fit(test_examples=test_data,
                test_latent_reps=test_latent_reps,
                n_keep=n_keep)
    explainers.append(nn_dist)

    # Save explainers and data:
    for explainer, explainer_name in zip(explainers, explainers_name):
        explainer_path = os.path.join(save_path, f'{explainer_name}_cv{cv}_n{n_keep}.pkl')
        with open(explainer_path, 'wb') as f:
            print(f'Saving {explainer_name} decomposition in {explainer_path}.')
            pkl.dump(explainer, f)
    corpus_data_path = os.path.join(save_path, f'corpus_data_cv{cv}.pkl')
    with open(corpus_data_path, 'wb') as f:
        print(f'Saving corpus data in {corpus_data_path}.')
        pkl.dump([corpus_latent_reps, corpus_probas, corpus_true_classes], f)
    test_data_path = os.path.join(save_path, f'test_data_cv{cv}.pkl')
    with open(test_data_path, 'wb') as f:
        print(f'Saving test data in {test_data_path}.')
        pkl.dump([test_latent_reps, test_targets], f)
    return explainers


# Fit a representer
def fit_representer(model_reg_factor, load_path: str, cv: int = 0):
    # Fit the representer explainer (this is only makes sense by using the whole corpus)
    corpus_data_path = os.path.join(load_path, f'corpus_data_cv{cv}.pkl')
    with open(corpus_data_path, 'rb') as f:
        corpus_latent_reps, corpus_probas, corpus_true_classes = pkl.load(f)
    test_data_path = os.path.join(load_path, f'test_data_cv{cv}.pkl')
    with open(test_data_path, 'rb') as f:
        test_latent_reps, test_targets = pkl.load(f)
    representer = Representer(corpus_latent_reps=corpus_latent_reps,
                              corpus_probas=corpus_probas,
                              corpus_true_classes=corpus_true_classes,
                              reg_factor=model_reg_factor)
    representer.fit(test_latent_reps=test_latent_reps)
    explainer_path = os.path.join(load_path, f'representer_cv{cv}.pkl')
    with open(explainer_path, 'wb') as f:
        print(f'Saving representer decomposition in {explainer_path}.')
        pkl.dump(representer, f)
    return representer


# Approximation Quality experiment
def approximation_quality(n_keep_list: list, cv: int = 0, random_seed: int = 42,
                          model_reg_factor=0.1, save_path: str = './results/mnist/quality/'):
    print(100 * '-' + '\n' + 'Welcome in the approximation quality experiment for MNIST. \n'
                             f'Settings: random_seed = {random_seed} ; cv = {cv}.\n'
          + 100 * '-')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    explainers_name = ['simplex', 'nn_uniform', 'nn_dist', 'representer']

    # Create saving directory if inexistent
    if not os.path.exists(save_path):
        print(f'Creating the saving directory {save_path}')
        os.makedirs(save_path)

    # Training a model, save it
    print(100 * '-' + '\n' + 'Now fitting the model. \n' + 100 * '-')
    train_model(device, random_seed=random_seed, cv=cv, save_path=save_path, model_reg_factor=model_reg_factor)

    # Load the model
    classifier = MnistClassifier()
    classifier.load_state_dict(torch.load(os.path.join(save_path, f'model_cv{cv}.pth')))
    classifier.to(device)
    classifier.eval()

    # Fit the explainers
    print(100 * '-' + '\n' + 'Now fitting the explainers. \n' + 100 * '-')
    for i, n_keep in enumerate(n_keep_list):
        print(100 * '-' + '\n' + f'Run number {i + 1}/{len(n_keep_list)} . \n' + 100 * '-')
        explainers = fit_explainers(device=device, random_seed=random_seed, cv=cv, test_size=100, corpus_size=1000,
                                    n_keep=n_keep, save_path=save_path, explainers_name=explainers_name)
        # Print the partial results
        print(100 * '-' + '\n' + 'Results. \n' + 100 * '-')
        for explainer, explainer_name in zip(explainers, explainers_name[:-1]):
            latent_rep_approx = explainer.latent_approx()
            latent_rep_true = explainer.test_latent_reps
            output_approx = classifier.latent_to_presoftmax(latent_rep_approx).detach()
            output_true = classifier.latent_to_presoftmax(latent_rep_true).detach()
            latent_r2_score = sklearn.metrics.r2_score(latent_rep_true.cpu().numpy(), latent_rep_approx.cpu().numpy())
            output_r2_score = sklearn.metrics.r2_score(output_true.cpu().numpy(), output_approx.cpu().numpy())
            print(f'{explainer_name} latent r2: {latent_r2_score:.2g} ; output r2 = {output_r2_score:.2g}.')

    # Fit the representer explainer (this is only makes sense by using the whole corpus)
    representer = fit_representer(model_reg_factor, save_path, cv)
    latent_rep_true = representer.test_latent_reps
    output_true = classifier.latent_to_presoftmax(latent_rep_true).detach()
    output_approx = representer.output_approx()
    output_r2_score = sklearn.metrics.r2_score(output_true.cpu().numpy(), output_approx.cpu().numpy())
    print(f'representer output r2 = {output_r2_score:.2g}.')


# Outlier Detection experiment
def outlier_detection(cv: int = 0, random_seed: int = 42, save_path: str = './results/mnist/outlier/'):
    torch.random.manual_seed(random_seed + cv)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(100 * '-' + '\n' + 'Welcome in the outlier detection experiment for MNIST. \n'
                             f'Settings: random_seed = {random_seed} ; cv = {cv}.\n'
          + 100 * '-')

    # Create saving directory if inexistent
    if not os.path.exists(save_path):
        print(f'Creating the saving directory {save_path}')
        os.makedirs(save_path)

    # Training a model, save it

    print(100 * '-' + '\n' + 'Now fitting the model. \n' + 100 * '-')
    train_model(device, random_seed=random_seed, cv=cv, save_path=save_path, model_reg_factor=0)

    # Load the model
    classifier = MnistClassifier()
    classifier.load_state_dict(torch.load(os.path.join(save_path, f'model_cv{cv}.pth')))
    classifier.to(device)
    classifier.eval()

    # Load data:
    corpus_loader = load_mnist(batch_size=1000, train=True)
    mnist_test_loader = load_mnist(batch_size=100, train=False)
    emnist_test_loader = load_emnist(batch_size=100, train=True)
    corpus_examples = enumerate(corpus_loader)
    batch_id_corpus, (corpus_data, corpus_target) = next(corpus_examples)
    corpus_data = corpus_data.to(device).detach()
    mnist_test_examples = enumerate(mnist_test_loader)
    batch_id_test_mnist, (mnist_test_data, mnist_test_target) = next(mnist_test_examples)
    mnist_test_data = mnist_test_data.to(device).detach()
    emnist_test_examples = enumerate(emnist_test_loader)
    batch_id_test_emnist, (emnist_test_data, emnist_test_target) = next(emnist_test_examples)
    emnist_test_data = emnist_test_data.to(device).detach()
    test_data = torch.cat([mnist_test_data, emnist_test_data], dim=0)
    corpus_latent_reps = classifier.latent_representation(corpus_data).detach()
    test_latent_reps = classifier.latent_representation(test_data).detach()

    # Fit corpus:
    reg_factor_scheduler = ExponentialScheduler(1, 1, n_epoch=1)
    corpus = Simplex(corpus_examples=corpus_data,
                     corpus_latent_reps=corpus_latent_reps)
    weights = corpus.fit(test_examples=test_data,
                         test_latent_reps=test_latent_reps,
                         n_epoch=10000, learning_rate=100.0, momentum=0.5,
                         reg_factor=0, n_keep=corpus_data.shape[0],
                         reg_factor_scheduler=reg_factor_scheduler)
    explainer_path = os.path.join(save_path, f'simplex_cv{cv}.pkl')
    with open(explainer_path, 'wb') as f:
        print(f'Saving representer decomposition in {explainer_path}.')
        pkl.dump(corpus, f)
    test_latent_approx = corpus.latent_approx()
    test_residuals = torch.sqrt(((test_latent_reps - test_latent_approx) ** 2).mean(dim=-1))
    n_inspected = [n for n in range(test_data.shape[0])]
    n_detected = [torch.count_nonzero(torch.topk(test_residuals, k=n)[1] > 99) for n in n_inspected]
    sns.set()
    plt.plot(n_inspected, n_detected)
    plt.xlabel('Number of inspected examples')
    plt.ylabel('Number of outliers detected')
    plt.show()


def main(experiment: str, cv: int):
    if experiment == 'approximation_quality':
        approximation_quality(cv=cv, n_keep_list=[n for n in range(2, 51)])
    elif experiment == 'outlier':
        outlier_detection(cv)


parser = argparse.ArgumentParser()
parser.add_argument('-experiment', type=str, default='approximation_quality', help='Experiment to perform')
parser.add_argument('-cv', type=int, default=0, help='Cross validation parameter')
args = parser.parse_args()

if __name__ == '__main__':
    main(args.experiment, args.cv)

'''

def approximation_quality_single(cv: int = 0, random_seed: int = 42, n_keep: int = 10, load_model: bool = False,
                                 model_reg_factor=0.1, save_path: str = './results/mnist/'):
    print(100 * '-' + '\n' + 'Welcome in the approximation quality experiment for MNIST. \n'
                             f'Settings: random_seed = {random_seed} ; n_keep = {n_keep} ; load_model = {load_model}.\n'
          + 100 * '-')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    explainers_name = ['corpus', 'nn_uniform', 'nn_dist', 'representer']

    # Create saving directory if inexistent
    if not os.path.exists(save_path):
        print(f'Creating the saving directory {save_path}')
        os.makedirs(save_path)

    # Training a model from scratch
    if not load_model:
        print(100 * '-' + '\n' + 'Now fitting the model. \n' + 100 * '-')
        train_model(device, random_seed=random_seed, cv=cv, save_path=save_path, model_reg_factor=model_reg_factor)

    # Load the model
    classifier = MnistClassifier()
    classifier.load_state_dict(torch.load(os.path.join(save_path, f'model_cv{cv}.pth')))
    classifier.to(device)
    classifier.eval()

    # Fit the explainers
    print(100 * '-' + '\n' + 'Now fitting the explainers. \n' + 100 * '-')
    explainers = fit_explainers(device=device, random_seed=random_seed, cv=cv, test_size=100, corpus_size=1000,
                                n_keep=n_keep, save_path=save_path, explainers_name=explainers_name,
                                model_reg_factor=model_reg_factor)

    # Print the partial results
    print(100 * '-' + '\n' + 'Results. \n' + 100 * '-')
    for explainer, explainer_name in zip(explainers, explainers_name):
        if not explainer_name == 'representer':
            latent_rep_approx = explainer.latent_approx()
            latent_rep_true = explainer.test_latent_reps
            output_approx = classifier.latent_to_presoftmax(latent_rep_approx).detach()
            output_true = classifier.latent_to_presoftmax(latent_rep_true).detach()
            latent_r2_score = sklearn.metrics.r2_score(latent_rep_true.cpu().numpy(), latent_rep_approx.cpu().numpy())
        else:
            latent_rep_true = explainer.test_latent_reps
            output_true = classifier.latent_to_presoftmax(latent_rep_true).detach()
            output_approx = explainer.output_approx()
            latent_r2_score = 0
        output_r2_score = sklearn.metrics.r2_score(output_true.cpu().numpy(), output_approx.cpu().numpy())
        print(f'{explainer_name} latent r2: {latent_r2_score:.2g} ; output r2 = {output_r2_score:.2g}.')

'''
