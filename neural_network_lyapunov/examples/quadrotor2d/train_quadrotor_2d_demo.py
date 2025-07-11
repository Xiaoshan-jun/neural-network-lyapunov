import neural_network_lyapunov.examples.quadrotor2d.quadrotor_2d as\
    quadrotor_2d
import neural_network_lyapunov.relu_system as relu_system
import neural_network_lyapunov.lyapunov as lyapunov
import neural_network_lyapunov.feedback_system as feedback_system
import neural_network_lyapunov.train_lyapunov_barrier as train_lyapunov_barrier
import neural_network_lyapunov.utils as utils
import neural_network_lyapunov.mip_utils as mip_utils
import neural_network_lyapunov.train_utils as train_utils
import neural_network_lyapunov.r_options as r_options

import torch
import numpy as np
import scipy.integrate
import gurobipy
import argparse
import os
import matplotlib.pyplot as plt


def generate_quadrotor_dynamics_data(dt):
    """
    Generate the pairs (x[n], u[n]) -> (x[n+1])
    """
    dtype = torch.float64
    plant = quadrotor_2d.Quadrotor2D(dtype)

    theta_range = [-np.pi / 2, np.pi / 2]
    ydot_range = [-5, 5]
    zdot_range = [-5, 5]
    thetadot_range = [-2.5, 2.5]
    u_range = [-0.5, 8.5]
    # We don't need to take the grid on y and z dimension of the quadrotor,
    # since the dynamics is invariant along these dimensions.
    x_samples = torch.cat((
        torch.zeros((1000, 2), dtype=torch.float64),
        utils.uniform_sample_in_box(
            torch.tensor([
                theta_range[0], ydot_range[0], zdot_range[0], thetadot_range[0]
            ],
                         dtype=torch.float64),
            torch.tensor([
                theta_range[1], ydot_range[1], zdot_range[1], thetadot_range[1]
            ],
                         dtype=torch.float64), 1000)),
                          dim=1).T
    u_samples = utils.uniform_sample_in_box(
        torch.full((2, ), u_range[0], dtype=dtype),
        torch.full((2, ), u_range[1], dtype=dtype), 1000).T

    xu_tensors = []
    x_next_tensors = []

    for i in range(x_samples.shape[1]):
        for j in range(u_samples.shape[1]):
            result = scipy.integrate.solve_ivp(
                lambda t, x: plant.dynamics(x, u_samples[:, j].detach().numpy(
                )), (0, dt), x_samples[:, i].detach().numpy())
            xu_tensors.append(
                torch.cat((x_samples[:, i], u_samples[:, j])).reshape((1, -1)))
            x_next_tensors.append(
                torch.from_numpy(result.y[:, -1]).reshape((1, -1)))
    dataset_input = torch.cat(xu_tensors, dim=0)
    dataset_output = torch.cat(x_next_tensors, dim=0)
    return torch.utils.data.TensorDataset(dataset_input, dataset_output)


def train_forward_model(forward_model, model_dataset, num_epochs):
    # The forward model maps (theta[n], u1[n], u2[n]) to
    # (ydot[n+1]-ydot[n], zdot[n+1]-zdot[n], thetadot[n+1]-thetadot[n])
    plant = quadrotor_2d.Quadrotor2D(torch.float64)
    u_equilibrium = plant.u_equilibrium

    xu_inputs, x_next_outputs = model_dataset[:]
    network_input_data = xu_inputs[:, [2, 5, 6, 7]]
    network_output_data = x_next_outputs[:, 3:] - xu_inputs[:, 3:6]
    v_dataset = torch.utils.data.TensorDataset(network_input_data,
                                               network_output_data)

    def compute_next_v(model, theta_thetadot_u):
        return model(theta_thetadot_u) - model(
            torch.cat(
                (torch.tensor([0, 0], dtype=torch.float64), u_equilibrium)))

    utils.train_approximator(v_dataset,
                             forward_model,
                             compute_next_v,
                             batch_size=50,
                             num_epochs=num_epochs,
                             lr=0.001)


def train_lqr_value_approximator(lyapunov_relu, V_lambda, R, x_equilibrium,
                                 x_lo, x_up, num_samples, lqr_S: torch.Tensor):
    """
    We train both lyapunov_relu and R such that ϕ(x) − ϕ(x*) + λ|R(x−x*)|₁
    approximates the lqr cost-to-go.
    """
    x_samples = utils.uniform_sample_in_box(x_lo, x_up, num_samples)
    V_samples = torch.sum((x_samples.T - x_equilibrium.reshape(
        (6, 1))) * (lqr_S @ (x_samples.T - x_equilibrium.reshape((6, 1)))),
                          dim=0).reshape((-1, 1))
    state_value_dataset = torch.utils.data.TensorDataset(x_samples, V_samples)
    R.requires_grad_(True)

    def compute_v(model, x):
        return model(x) - model(x_equilibrium) + V_lambda * torch.norm(
            R @ (x - x_equilibrium.reshape((1, 6))).T, p=1, dim=0).reshape(
                (-1, 1))

    utils.train_approximator(state_value_dataset,
                             lyapunov_relu,
                             compute_v,
                             batch_size=50,
                             num_epochs=200,
                             lr=0.001,
                             additional_variable=[R])
    R.requires_grad_(False)


def train_lqr_control_approximator(controller_relu, x_equilibrium,
                                   u_equilibrium, x_lo, x_up, num_samples,
                                   lqr_K: torch.Tensor):
    x_samples = utils.uniform_sample_in_box(x_lo, x_up, num_samples)
    u_samples = (lqr_K @ (x_samples.T - x_equilibrium.reshape(
        (6, 1))) + u_equilibrium.reshape((2, 1))).T
    state_control_dataset = torch.utils.data.TensorDataset(
        x_samples, u_samples)

    def compute_u(model, x):
        return model(x) - model(x_equilibrium) + u_equilibrium

    utils.train_approximator(state_control_dataset,
                             controller_relu,
                             compute_u,
                             batch_size=50,
                             num_epochs=50,
                             lr=0.001)


def train_nn_controller_approximator(controller_relu, target_controller_relu,
                                     x_lo, x_up, num_samples, num_epochs):
    x_samples = utils.uniform_sample_in_box(x_lo, x_up, num_samples)
    target_controller_relu_output = target_controller_relu(x_samples)
    dataset = torch.utils.data.TensorDataset(x_samples,
                                             target_controller_relu_output)

    def compute_output(model, x):
        return model(x)

    utils.train_approximator(dataset,
                             controller_relu,
                             compute_output,
                             batch_size=50,
                             num_epochs=num_epochs,
                             lr=0.001)


def train_nn_lyapunov_approximator(lyapunov_relu, R, target_lyapunov_relu,
                                   target_R, V_lambda, x_equilibrium, x_lo,
                                   x_up, num_samples, num_epochs):
    x_samples = utils.uniform_sample_in_box(x_lo, x_up, num_samples)
    with torch.no_grad():
        target_V = target_lyapunov_relu(x_samples) - target_lyapunov_relu(
            x_equilibrium) + V_lambda * torch.norm(
                target_R @ (x_samples - x_equilibrium).T, p=1, dim=0).reshape(
                    (-1, 1))

    dataset = torch.utils.data.TensorDataset(x_samples, target_V)

    def compute_V(model, x):
        return model(x) - model(x_equilibrium) + V_lambda * torch.norm(
            R @ (x - x_equilibrium).T, p=1, dim=0).reshape((-1, 1))

    R.requires_grad_(True)
    utils.train_approximator(dataset,
                             lyapunov_relu,
                             compute_V,
                             batch_size=50,
                             num_epochs=num_epochs,
                             lr=0.001,
                             additional_variable=[R])
    R.requires_grad_(False)


def simulate_quadrotor_with_controller(controller_relu, t_span, x_equilibrium,
                                       u_lo, u_up, x0):
    plant = quadrotor_2d.Quadrotor2D(torch.float64)
    u_equilibrium = plant.u_equilibrium

    def dyn(t, x):
        with torch.no_grad():
            x_torch = torch.from_numpy(x)
            u_torch = controller_relu(x_torch)\
                - controller_relu(x_equilibrium) + u_equilibrium
            u = torch.max(torch.min(u_torch, u_up), u_lo).detach().numpy()
        return plant.dynamics(x, u)

    result = scipy.integrate.solve_ivp(dyn,
                                       t_span,
                                       x0,
                                       t_eval=np.arange(start=t_span[0],
                                                        stop=t_span[1],
                                                        step=0.01))
    return result


def evaluate_controller(controller, x0_grid, t_span, threshold=0.1):
    x_equilibrium = torch.zeros((6,), dtype=torch.float64)
    u_lo = torch.tensor([-8, -8], dtype=torch.float64)
    u_up = torch.tensor([8, 8], dtype=torch.float64)

    success_mask = []
    for x0 in x0_grid:
        result = simulate_quadrotor_with_controller(controller, t_span, x_equilibrium, u_lo, u_up, x0)
        final_state = result.y[:, -1]
        dist = np.linalg.norm(final_state - x_equilibrium.numpy())
        success_mask.append(dist < threshold)
    return success_mask


def plot_stabilization_result(x0_grid, success_mask, title="Stabilization Result", projection=(3, 4)):
    """
    projection: tuple of indices to plot, e.g., (2, 5) for (theta, thetadot)
    """
    x = np.array([x[p] for x in x0_grid for p in [projection[0]]])
    y = np.array([x[p] for x in x0_grid for p in [projection[1]]])
    colors = ['green' if s else 'red' for s in success_mask]

    plt.figure(figsize=(8, 6))
    plt.scatter(x, y, c=colors, s=10, alpha=0.8)
    plt.xlabel(f"$x[{projection[0]}]$")
    plt.ylabel(f"$x[{projection[1]}]$")
    plt.title(title)
    plt.grid(True)
    plt.show()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="quadrotor 2d training demo")
    parser.add_argument("--generate_dynamics_data", action="store_true", default= True)
    parser.add_argument("--load_dynamics_data",
                        type=str,
                        default="/data/dynamic.pt",
                        help="path of the dynamics data")
    parser.add_argument("--train_forward_model", action="store_true", default = True)
    parser.add_argument("--load_forward_model",
                        type=str,
                        default='/data/second_order_forward_relu1.pt',
                        help="path of the forward model")
    parser.add_argument("--load_lyapunov_relu",
                        type=str,
                        default="/data/lyapunov4.pt",
                        help="path to the lyapunov model data.")
    parser.add_argument("--load_controller_relu",
                        type=str,
                        default="/data/controller4.pt",
                        help="path to the controller data.")
    parser.add_argument("--train_lqr_approximator", action="store_true", default = False)
    parser.add_argument("--search_R", action="store_true", default = True)
    parser.add_argument("--train_on_samples", action="store_true", default = True)
    parser.add_argument("--enable_wandb", action="store_true", default = False)
    parser.add_argument("--train_adversarial", action="store_true", default = True)
    parser.add_argument("--max_iterations", type=int, default=5000)
    parser.add_argument("--training_set", type=str, default=None)
    args = parser.parse_args()
    dir_path = os.path.dirname(os.path.realpath(__file__))
    dt = 0.01 #integration time step, simulating the system by 0.01 second every step
    dtype = torch.float64
    if args.generate_dynamics_data:
        print("generate dynamic dataset")
        model_dataset = generate_quadrotor_dynamics_data(dt)
        torch.save((model_dataset.tensors[0], model_dataset.tensors[1]), dir_path + "/data/dynamic.pt")
    elif args.load_dynamics_data is not None:
        model_dataset = torch.load(dir_path + args.load_dynamics_data)
    #------------------------------------train the dynamic model----------------------------------------------------
    if args.train_forward_model:
        print('initialize forward nn model')
        forward_model = utils.setup_relu((4, 6, 6, 3),
                                         params=None,
                                         bias=True,
                                         negative_slope=0.01,
                                         dtype=dtype)
        train_forward_model(forward_model, model_dataset, num_epochs=10)
    elif args.load_forward_model:
        forward_model_data = torch.load(dir_path + args.load_forward_model)
        forward_model = utils.setup_relu(
            forward_model_data["linear_layer_width"],
            params=None,
            bias=forward_model_data["bias"],
            negative_slope=forward_model_data["negative_slope"],
            dtype=dtype)
        forward_model.load_state_dict(forward_model_data["state_dict"])
    # ------------------------------------train the dynamic model----------------------------------------------------
    plant = quadrotor_2d.Quadrotor2D(dtype) #real dynamic simulator
    x_star = np.zeros((6, )) #state equilibrium
    u_star = plant.u_equilibrium.detach().numpy() # control equilibrium, against the gravity
    lqr_Q = np.diag([10, 10, 10, 1, 1, plant.length / 2. / np.pi]) #for LQR
    lqr_R = np.array([[0.1, 0.05], [0.05, 0.1]]) #for LQR
    K, S = plant.lqr_control(lqr_Q, lqr_R, x_star, u_star) #feedback gain and solution to the continuous-time Algebraic Riccati Equation, gives the cost-to-go approximation
    S_eig_value, S_eig_vec = np.linalg.eig(S) #Computes the eigenvalues and eigenvectors of the matrix SS, which are useful for

    # R = torch.zeros((9, 6), dtype=dtype)
    # R[:3, :3] = torch.eye(3, dtype=dtype)
    # R[3:6, :3] = torch.eye(3, dtype=dtype) / np.sqrt(2)
    # R[3:6, 3:6] = torch.eye(3, dtype=dtype) / np.sqrt(2)
    # R[6:9, :3] = -torch.eye(3, dtype=dtype) / np.sqrt(2)
    # R[6:9, 3:6] = torch.eye(3, dtype=dtype) / np.sqrt(2)
    # R = torch.cat((R, torch.from_numpy(S_eig_vec)), dim=0)
    R = torch.from_numpy(S) + 0.01 * torch.eye(6, dtype=dtype)

    lyapunov_relu = utils.setup_relu((6, 10, 10, 4, 1),
                                     params=None,
                                     negative_slope=0.1,
                                     bias=True,
                                     dtype=dtype)
    V_lambda = 0.9
    if args.load_lyapunov_relu is not None:
        lyapunov_data = torch.load(dir_path + args.load_lyapunov_relu)
        lyapunov_relu = utils.setup_relu(
            lyapunov_data["linear_layer_width"],
            params=None,
            negative_slope=lyapunov_data["negative_slope"],
            bias=lyapunov_data["bias"],
            dtype=dtype)
        lyapunov_relu.load_state_dict(lyapunov_data["state_dict"])
        V_lambda = lyapunov_data["V_lambda"]
        R = lyapunov_data["R"]

    controller_relu = utils.setup_relu((6, 6, 4, 2),
                                       params=None,
                                       negative_slope=0.01,
                                       bias=True,
                                       dtype=dtype)
    if args.load_controller_relu is not None:
        controller_data = torch.load(dir_path + args.load_controller_relu)
        controller_relu = utils.setup_relu(
            controller_data["linear_layer_width"],
            params=None,
            negative_slope=controller_data["negative_slope"],
            bias=controller_data["bias"],
            dtype=dtype)
        controller_relu.load_state_dict(controller_data["state_dict"])


    q_equilibrium = torch.tensor([0, 0, 0], dtype=dtype)
    u_equilibrium = plant.u_equilibrium
    x_lo = torch.tensor([-0.7, -0.7, -np.pi * 0.5, -3.75, -3.75, -2.5],
                        dtype=dtype)
    x_up = -x_lo
    u_lo = torch.tensor([0, 0], dtype=dtype)
    u_up = torch.tensor([8, 8], dtype=dtype)
    if args.enable_wandb:
        train_utils.wandb_config_update(args, lyapunov_relu, controller_relu,
                                        x_lo, x_up, u_lo, u_up)


    if args.train_lqr_approximator:
        x_equilibrium = torch.cat(
            (q_equilibrium, torch.zeros((3, ), dtype=dtype)))
        print('training controller')
        train_lqr_control_approximator(controller_relu, x_equilibrium,
                                       u_equilibrium, x_lo, x_up, 100000,
                                       torch.from_numpy(K))
        print('training lyapunov_relu')
        train_lqr_value_approximator(lyapunov_relu, V_lambda, R, x_equilibrium,
                                     x_lo, x_up, 100000, torch.from_numpy(S))
    forward_system = relu_system.ReLUSecondOrderResidueSystemGivenEquilibrium(
        dtype,
        x_lo,
        x_up,
        u_lo,
        u_up,
        forward_model,
        q_equilibrium,
        u_equilibrium,
        dt,
        network_input_x_indices=[2, 5])
    closed_loop_system = feedback_system.FeedbackSystem(
        forward_system, controller_relu, forward_system.x_equilibrium,
        forward_system.u_equilibrium,
        u_lo.detach().numpy(),
        u_up.detach().numpy())
    lyap = lyapunov.LyapunovDiscreteTimeHybridSystem(closed_loop_system,
                                                     lyapunov_relu)

    if args.search_R:
        _, R_sigma, _ = np.linalg.svd(R.detach().numpy())
        R_options = r_options.SearchRwithSVDOptions(R.shape, R_sigma * 0.8)
        R_options.set_variable_value(R.detach().numpy())
    else:
        R_options = r_options.FixedROptions(R)
    dut = train_lyapunov_barrier.Trainer() #lyapunoc barrier trainer
    dut.add_lyapunov(lyap, V_lambda, closed_loop_system.x_equilibrium,
                     R_options)
    dut.lyapunov_positivity_mip_pool_solutions = 1
    dut.lyapunov_derivative_mip_pool_solutions = 1
    dut.lyapunov_derivative_convergence_tol = 1E-5
    dut.lyapunov_positivity_convergence_tol = 5e-6
    dut.max_iterations = args.max_iterations
    dut.lyapunov_positivity_epsilon = 0.1
    dut.lyapunov_derivative_epsilon = 0.001
    dut.lyapunov_derivative_eps_type = lyapunov.ConvergenceEps.ExpLower
    state_samples_all = utils.get_meshgrid_samples(x_lo,
                                                   x_up, (7, 7, 7, 7, 7, 7),
                                                   dtype=dtype)
    dut.output_flag = True
    if args.train_on_samples:
        dut.train_lyapunov_on_samples(state_samples_all,
                                      num_epochs=10,
                                      batch_size=50)
    dut.enable_wandb = args.enable_wandb
    if args.train_adversarial:
        options = train_lyapunov_barrier.Trainer.AdversarialTrainingOptions()
        options.num_batches = 10
        options.num_epochs_per_mip = 20
        options.positivity_samples_pool_size = 10000
        options.derivative_samples_pool_size = 100000
        dut.lyapunov_positivity_mip_pool_solutions = 100
        dut.lyapunov_derivative_mip_pool_solutions = 500
        dut.add_derivative_adversarial_state = True
        dut.add_positivity_adversarial_state = True
        forward_system.network_bound_propagate_method =\
            mip_utils.PropagateBoundsMethod.MIP
        dut.lyapunov_hybrid_system.network_bound_propagate_method =\
            mip_utils.PropagateBoundsMethod.MIP
        closed_loop_system.controller_network_bound_propagate_method =\
            mip_utils.PropagateBoundsMethod.MIP
        dut.lyapunov_derivative_mip_params = {
            gurobipy.GRB.Param.OutputFlag: False
        }
        if args.training_set:
            training_set_data = torch.load(args.training_set)
            positivity_state_samples_init = training_set_data[
                "positivity_state_samples"]
            derivative_state_samples_init = training_set_data[
                "derivative_state_samples"]
        else:
            positivity_state_samples_init = utils.uniform_sample_in_box(
                x_lo, x_up, 1000)
            derivative_state_samples_init = positivity_state_samples_init
        result = dut.train_adversarial(positivity_state_samples_init,
                                       derivative_state_samples_init, options)
    else:
        dut.train(torch.empty((0, 6), dtype=dtype))
    # test controller
    # Generate a grid of initial state)
    xdot_samples = np.random.uniform(-8, 8, 50)
    zdot_samples = np.random.uniform(-8, 8, 50)
    x0_grid = [np.array([-0.75, 0.3, 0.3*np.pi, xdot, zdot, 2]) for xdot in xdot_samples for zdot in zdot_samples]

    # Assume controller_relu is already loaded
    t_span = (0, 5.0)
    success_mask = evaluate_controller(controller_relu, x0_grid, t_span)

    # Plot
    plot_stabilization_result(x0_grid, success_mask)


    pass
