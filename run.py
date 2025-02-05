import tensorflow as tf
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
session = tf.Session(config=config)
from keras import models, layers, datasets, utils, backend, optimizers, initializers
from transformations import get_transformations
import PIL.Image
import numpy as np
import time
from IPython import embed
# datasets in the AutoAugment paper:
# CIFAR-10, CIFAR-100, SVHN, and ImageNet
# SVHN = http://ufldl.stanford.edu/housenumbers/

def get_dataset(dataset, reduced):
    if dataset == 'cifar10':
        (Xtr, ytr), (Xts, yts) = datasets.cifar10.load_data()
    elif dataset == 'cifar100':
        (Xtr, ytr), (Xts, yts) = datasets.cifar100.load_data()
    elif dataset == 'headwear':
        Xtr = np.load('Xtr.npy')
        ytr = np.load('ytr.npy')
        Xts = np.load('Xts.npy')
        yts = np.load('yts.npy')
        return (Xtr, ytr), (Xts, yts)
    else:
        raise Exception('Unknown dataset %s' % dataset)
    if reduced:
        ix = np.random.choice(len(Xtr), 4000, False)
        Xtr = Xtr[ix]
        ytr = ytr[ix]
    ytr = utils.to_categorical(ytr)
    yts = utils.to_categorical(yts)
    return (Xtr, ytr), (Xts, yts)

(Xtr, ytr), (Xts, yts) = get_dataset('headwear', True)
transformations = get_transformations(Xtr)

# Experiment parameters

LSTM_UNITS = 100

SUBPOLICIES = 5
SUBPOLICY_OPS = 2

OP_TYPES = 16
OP_PROBS = 11
OP_MAGNITUDES = 10

CHILD_BATCH_SIZE = 128
CHILD_BATCHES = len(Xtr) // CHILD_BATCH_SIZE
CHILD_EPOCHS =50
CONTROLLER_EPOCHS = 15000 # 15000 or 20000

class Operation:
    def __init__(self, types_softmax, probs_softmax, magnitudes_softmax, argmax=False):
        # Ekin Dogus says he sampled the softmaxes, and has not used argmax
        # We might still want to use argmax=True for the last predictions, to ensure
        # the best solutions are chosen and make it deterministic.
        if argmax:
            self.type = types_softmax.argmax()
            t = transformations[self.type]
            self.prob = probs_softmax.argmax() / (OP_PROBS-1)
            m = magnitudes_softmax.argmax() / (OP_MAGNITUDES-1)
            self.magnitude = m*(t[2]-t[1]) + t[1]
        else:
            self.type = np.random.choice(OP_TYPES, p=types_softmax[0])
            t = transformations[self.type]
            self.prob = np.random.choice(np.linspace(0, 1, OP_PROBS), p=probs_softmax[0])
            self.Mag = np.random.choice(10, p=magnitudes_softmax[0])
            self.magnitude = np.linspace(t[1], t[2], OP_MAGNITUDES)[self.Mag]
        self.transformation = t[0]



    def __call__(self, X):
        _X = []
        for x in X:
            if np.random.rand() < self.prob:
                x = PIL.Image.fromarray(x)
                x = self.transformation(x, self.magnitude)
            _X.append(np.array(x))
        return np.array(_X)

    def __str__(self):
        return 'Operation %2d (P=%.3f, M=%.3f)' % (self.type, self.prob, self.magnitude)

class Subpolicy:
    def __init__(self, *operations):
        self.operations = operations

    def __call__(self, X):
        for op in self.operations:
            X = op(X)
        return X

    def __str__(self):
        ret = ''
        for i, op in enumerate(self.operations):
            ret += str(op)
            if i < len(self.operations)-1:
                ret += '\n'
        return ret

class Controller:
    def __init__(self):
        self.model = self.create_model()
        self.grads = tf.gradients(self.model.outputs, self.model.trainable_weights)
        # negative for gradient ascent
        #self.grads = [g * (-self.scale) for g in self.grads]
        self.grads = zip(self.grads, self.model.trainable_weights)
        self.optimizer = tf.train.GradientDescentOptimizer(0.00035).apply_gradients(self.grads)
        self.session = backend.get_session()
    def sli(self, x, i, a):
        return x[:,i*3+a:i*3+1+a,:]
    def create_model(self):
        # Implementation note: Keras requires an i  nput. I create an input and then feed
        # zeros to the network. Ugly, but it's the same as disabling those weights.
        # Furthermore, Keras LSTM input=output, so we cannot produce more than SUBPOLICIES
        # outputs. This is not desirable, since the paper produces 25 subpolicies in the
        # end.
        input_layer = layers.Input(shape=(30, 1))
        init = initializers.RandomUniform(-0.1, 0.1)
        lstm_layer = layers.LSTM(
            LSTM_UNITS, recurrent_initializer=init, return_sequences=True,
            name='controller')(input_layer)
        outputs = []
        for i in range(10):
            name = 'op%d-' % (i+1)
            outputs += [
                layers.Dense(OP_TYPES, activation='softmax', name=name + 't')(layers.core.Lambda(self.sli, arguments={'i':i, 'a':0,})(lstm_layer)),
                layers.Dense(OP_PROBS, activation='softmax', name=name + 'p')(layers.core.Lambda(self.sli, arguments={'i':i, 'a':1,})(lstm_layer)),
                layers.Dense(OP_MAGNITUDES, activation='softmax', name=name + 'm')(layers.core.Lambda(self.sli, arguments={'i':i, 'a':2,})(lstm_layer)),
            ]
        return models.Model(input_layer, outputs)

    def fit(self, mem_softmaxes, mem_accuracies, mem_Types):
        session = self.session
        min_acc = np.min(mem_accuracies)
        max_acc = np.max(mem_accuracies)
        mid_acc = (min_acc + max_acc) / 2
        dummy_input = np.zeros((1, 30, 1))
        dict_input = {self.model.input: dummy_input}
        dict_outputs = {}
        # FIXME: the paper does mini-batches (10)
        for softmaxes, acc , Types in zip(mem_softmaxes, mem_accuracies, mem_Types):
            for i in range(30):
                scale = (acc-mid_acc) / (max_acc-min_acc)
                dummpy_output = np.zeros((1,1,softmaxes[i].shape[2])) 
                dummpy_output[:,:,int(Types[i])] = (backend.log(softmaxes[i][:,:,int(Types[i])])*-scale).eval(session=session)
                dict_outputs[self.model.outputs[i]] = dummpy_output
            #dict_outputs = {_output: s for _output, s in zip(self.model.outputs, softmaxes)}
            session.run(self.optimizer, feed_dict={**dict_outputs,  **dict_input})
        return self

    def predict(self, size):
        dummy_input = np.zeros((1, size, 1), np.float32)
        softmaxes = self.model.predict(dummy_input)
        # convert softmaxes into subpolicies
        subpolicies = []
        Types=[]
        k = 0
        for i in range(5):
            operations = []
            for j in range(2):
                op = softmaxes[k:k+3]
                k+=3
                op = [op[0][0], op[1][0], op[2][0]]
                operations.append(Operation(*op))
                Types.extend([operations[-1].type, int(operations[-1].prob*10), operations[-1].Mag])
            subpolicies.append(Subpolicy(*operations))
        return softmaxes, subpolicies, Types

# generator
def autoaugment(subpolicies, X, y):
    while True:
        ix = np.arange(len(X))
        np.random.shuffle(ix)
        for i in range(CHILD_BATCHES):
            _ix = ix[i*CHILD_BATCH_SIZE:(i+1)*CHILD_BATCH_SIZE]
            _X = X[_ix]
            _y = y[_ix]
            subpolicy = np.random.choice(subpolicies)
            _X = subpolicy(_X)
            _X = _X.astype(np.float32) / 255
            yield _X, _y

class Child:
    # architecture from: https://github.com/keras-team/keras/blob/master/examples/mnist_cnn.py
    def __init__(self, input_shape):
        #backend.clear_session()
        self.model = self.create_model(input_shape)
        optimizer = optimizers.SGD(decay=1e-4)
        self.model.compile(optimizer, 'categorical_crossentropy', ['accuracy'])
        self.model.save_weights('./init_child.h5')

    def reinit(self):
        self.model.load_weights('./init_child.h5')

    def create_model(self, input_shape):
        x = input_layer = layers.Input(shape=input_shape)
        x = layers.Conv2D(32, 3, activation='relu')(x)
        x = layers.Conv2D(64, 3, activation='relu')(x)
        x = layers.MaxPooling2D(2)(x)
        x = layers.Dropout(0.25)(x)
        x = layers.Flatten()(x)
        x = layers.Dense(128, activation='relu')(x)
        x = layers.Dropout(0.5)(x)
        x = layers.Dense(4, activation='softmax')(x)
        return models.Model(input_layer, x)

    def fit(self, subpolicies, X, y):
        gen = autoaugment(subpolicies, X, y)
        self.model.fit_generator(
            gen, CHILD_BATCHES, CHILD_EPOCHS, verbose=0, use_multiprocessing=True)
        return self

    def evaluate(self, X, y):
        return self.model.evaluate(X, y, verbose=0)[1]

    def del_model(self):
        del self.model


mem_softmaxes = []
mem_accuracies = []
mem_Types = []
backend.set_session(session)
controller = Controller()
child = Child(Xtr.shape[1:])
for epoch in range(CONTROLLER_EPOCHS):
    print('Controller: Epoch %d / %d' % (epoch+1, CONTROLLER_EPOCHS))

    softmaxes, subpolicies, Types = controller.predict(30)
    for i, subpolicy in enumerate(subpolicies):
        print('# Sub-policy %d' % (i+1))
        print(subpolicy)
    mem_softmaxes.append(softmaxes)
    mem_Types.append(Types)
    #child = Child(Xtr.shape[1:])
    child.reinit()
    tic = time.time()
    child.fit(subpolicies, Xtr, ytr)
    toc = time.time()
    accuracy = child.evaluate(Xts, yts)
    #del child
    print('-> Child accuracy: %.3f (elaspsed time: %ds)' % (accuracy, (toc-tic)))
    mem_accuracies.append(accuracy)

    if len(mem_softmaxes) > 5:
        # ricardo: I let some epochs pass, so that the normalization is more robust
        controller.fit(mem_softmaxes, mem_accuracies, mem_Types)
        print("batch_acc:" ,np.mean(mem_accuracies))
        mem_softmaxes = []
        mem_accuracies = []
        mem_Types = []
    print()

print()
print('Best policies found:')
print()
policies=[]
for i in range(5):
    _, subpolicies = controller.predict(5)
    policies.append(subpolicies)
for i, subpolicy in enumerate(policies):
    print('# Subpolicy %d' % (i+1))
    for ss in subpolicy:
        print(ss)
