"""Potentiation/depression fitting and Bayesian optimization for probot.

Ported from the original ``HT_PotDep.py``. Heavy (torch/botorch/gpytorch) imports
are why this module is imported lazily by the measurement routines. I/O paths are
configurable via the module-level ``PARAM_DIR`` / ``DATA_DIR`` (set by
``probot_measurement`` before each call).
"""
#HT workflow for potentiation and depression
import pandas as pd
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
import matplotlib.pyplot as plt
import re
import seaborn as sns
from matplotlib.cm import get_cmap
import math
import numpy as np
import importlib
import sys
import matplotlib.colors as mcolors

from scipy.optimize import curve_fit
#from keysight import KeysightInstrument


import torch
from botorch.utils.transforms import normalize, standardize, unnormalize
from botorch.models import SingleTaskGP
from botorch.models.model_list_gp_regression import ModelListGP
from botorch.acquisition.multi_objective.monte_carlo import qExpectedHypervolumeImprovement
from botorch.optim import optimize_acqf
from botorch.utils.multi_objective.box_decompositions.non_dominated import NondominatedPartitioning
from gpytorch.mlls.sum_marginal_log_likelihood import SumMarginalLogLikelihood
from botorch.fit import fit_gpytorch_mll

# --- probot: configurable I/O locations (set by probot_measurement before use) ---
PARAM_DIR = "Parameters"
DATA_DIR = os.path.join("Data", "Keysight")


def _param_file(name):
    """Resolve a parameter CSV inside the configurable parameter directory."""
    return os.path.join(PARAM_DIR, name)


def _bo_dir():
    """Return the Bayesian-optimization data directory (trailing separator)."""
    d = os.path.join(DATA_DIR, "2_BO_data")
    os.makedirs(d, exist_ok=True)
    return d + os.sep


def make_results_summary(df):
    
    df_results = df
    ###########
    # make df of parameters
    #############
    #take column 'Parameter' and 'Value' from df_results and transpose it
    df_results_param = df_results[['Parameter', 'Value']]
    df_results_param = df_results_param.dropna().T
    df_results_param.columns = df_results_param.iloc[0]
    df_results_param = df_results_param.drop(df_results_param.index[0])

    #get rid of 'reset_period', 'pulse_no', 'cycle_write_erase', 'compliance' rows
    df_results_param = df_results_param.drop(['reset_period', 'pulse_no', 'cycle_write_erase', 'compliance'], axis=1)

    #get rid of the 'Parameter' row 
    df_results_param = df_results_param.reset_index(drop=True)
    #move column 'cell_number' to the first column
    df_results_param = df_results_param[['cell_number'] + [col for col in df_results_param.columns if col != 'cell_number']]



    ##########
    # make df of fitting results
    ##########
    df_fitting = df_results[['Meas_Cycle', 'Pot_S_init','Pot_S_final','Pot_gain', 'Pot_alpha','Pot_gamma', 'Pot_R2', 'Dep_S_init','Dep_S_final','Dep_gain', 'Dep_alpha', 'Dep_gamma', 'Dep_R2']]
    df_fitting = df_fitting.dropna()
    
    max_cycle = df_fitting['Meas_Cycle'].max()
    
    #prepare parameter tobe concat
    if (max_cycle > 2):
        df_results_param = pd.concat([df_results_param]*3, ignore_index=True)
    else:
        df_results_param = pd.concat([df_results_param]*int(max_cycle), ignore_index=True)
    
    #pepare fitting results to be concated

    if (max_cycle>2):
        df_fitting = df_fitting[df_fitting['Meas_Cycle'] > max_cycle-3] 
        
    else:
        df_fitting = df_fitting[df_fitting['Meas_Cycle'] > 0] 
        
    ''' no need. lets use all data
    #make df average 
    df_fitting_ave = df_fitting.mean()
    df_fitting_ave = df_fitting_ave.round(2)
    df_fitting_ave = pd.DataFrame(df_fitting_ave).T
    df_fitting_ave.columns = df_fitting.columns
    '''


    ##########
    #concat df_results_param, df_fitting_ave  
    #########
    df_results_summary = pd.concat([df_results_param, df_fitting], axis=1)
    #add timestamp
    df_results_summary['timestamp'] = pd.Timestamp.now()

    ##########
    #save the df_results_summary to csv
    #########
    folder_BO = _bo_dir()
    cell_number = int(df_results_summary['cell_number'][0])
    # make csv file with the name {cell_number}_potentiation_depression_summary.csv
    # if the file already exist then append the new data
    # if the header is not exis in the csv file, make the header. but if the header is exist then only append the data
    try:
        #try to open the file
        df_summary = pd.read_csv(folder_BO+f'{cell_number}_potentiation_depression_summary.csv')
        #if the file exist then append the new data
        df_results_summary.to_csv(folder_BO+f'{cell_number}_potentiation_depression_summary.csv', mode='a', header=False, index=False)
    except:
        #if the file not exist then make the file
        df_summary = df_results_summary
        df_summary.to_csv(folder_BO+f'{cell_number}_potentiation_depression_summary.csv', header=True, index=False)   
    print('Summary saved')

# --- Define equations for fitting ---
def potentiation(pulse, alpha_p, gamma_p):
    return 1 - (1 + alpha_p * (gamma_p - 1) * pulse) ** (1 / (1 - gamma_p))

def depression(pulse, alpha_d, gamma_d):
    return (1 + alpha_d * (gamma_d - 1) * pulse) ** (1 / (1 - gamma_d))

# --- Fitting function ---
def fit_data_pot(df, equation):
    #skip the first 9 data
    #df = df.iloc[9:]
    
    pls = df['pulse']
    pulse = (pls - pls.min()) / (pls.max() - pls.min())  # normalize pulse
    #pulse = pls
    G = df['Conductance (S)']
    G_min, G_max = G.min(), G.max()
    G_normalized = (G - G_min) / (G_max - G_min)

    #see if it potentiating or depressing
    if G_normalized.iloc[0] <= G_normalized.iloc[-1]:
        print('Potentiation')
        #if it is depressing, change the equation
        equation = equation
        bounds = ([0, -0.5], [100, 100])
        #G_normalized = 1 - G_normalized
    else:
        print('Depression')
        #if it is potentiating, change the equation
        equation = depression
        bounds = ([0, 0.01], [10, 10]) 
    
    try:
        initial_guess = [G_normalized.iloc[0], 2]

        try:
            # firs try with bound
            #bounds = ([0, -0.5], [100, 100])
            params, _ = curve_fit(equation, pulse, G_normalized, p0=initial_guess, bounds=bounds)
            G_fit = equation(pulse, *params)
            print("[Potentiation] Fit with bounds success")
        except:
            print("[Potentiation] Error in fitting with bounds. Trying without bounds...")
            params, _ = curve_fit(equation, pulse, G_normalized, p0=initial_guess)
            G_fit = equation(pulse, *params)

        # R^2 calculation
        residuals = G_normalized - G_fit
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((G_normalized - np.mean(G_normalized)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)

        return pulse, G_normalized, G_fit, params, r_squared
    except:
        print('Potentiation fitting totally failed')
        params = [0.0123456,0.0123456] #mark with this number for failed fitting
        G_fit = [0.0123456] * len(pulse)
        r_squared = 0.0123456

        return pulse, G_normalized, G_fit, params,r_squared
    
def fit_data_dep(df, equation):
    #skip the first 2 data
    #df = df.iloc[2:]

    pls = df['pulse']
    pulse = (pls - pls.min()) / (pls.max() - pls.min())  # normalize pulse
    #pulse = pls
    G = df['Conductance (S)']
    G_min, G_max = G.min(), G.max()
    G_normalized = (G - G_min) / (G_max - G_min)


    #see if it potentiating or depressing
    if G_normalized.iloc[0] >= G_normalized.iloc[-1]:
        print('Depression')
        #if it is depressing, change the equation
        equation = equation
        bounds = ([0, 0.01], [10, 10]) 
        
        #G_normalized = 1 - G_normalized
    else:
        print('Potentiation')
        #if it is potentiating, change the equation
        equation = potentiation
        bounds = ([0, -0.5], [100, 100])



    try:
        initial_guess = [G_normalized.iloc[0], 2]
        try:
            # firs try with bound
            #bounds = ([0, 0.01], [10, 10])  # alpha_p >= 0, gamma_p between 0.01 and 10
            params, _ = curve_fit(equation, pulse, G_normalized, p0=initial_guess, bounds=bounds)
            G_fit = equation(pulse, *params)
            print('[Depression] Fit with bounds success')
        except:
            print("[Depression] Error in fitting with bounds. Trying without bounds...")
            params, _ = curve_fit(equation, pulse, G_normalized, p0=initial_guess)
            G_fit = equation(pulse, *params)

        # R^2 calculation
        residuals = G_normalized - G_fit
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((G_normalized - np.mean(G_normalized)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)

        return pulse, G_normalized, G_fit, params, r_squared
    except: #return empty data if fitting failed
        print('Depression fitting totally failed')
        
        params = [0.0123,0.0123]
        G_fit = [0.0123] * len(pulse)
        r_squared = 0.0123
        
        return pulse, G_normalized, G_fit, params,r_squared

# --- Combined plotting function ---
def plot_pot_dep(df_cycle_pot, pulse_pot, G_norm_pot, G_fit_pot, params_pot, r2_pot,
                 df_cycle_dep, pulse_dep, G_norm_dep, G_fit_dep, params_dep, r2_dep, title):
    
    df_cycle_pot = df_cycle_pot.reset_index(drop=True)
    df_cycle_dep = df_cycle_dep.reset_index(drop=True)

    plt.figure(figsize=(6, 4))

    try:
        gain_pot = (df_cycle_pot['Conductance (S)'].iloc[-1]-df_cycle_pot['Conductance (S)'].iloc[0])/df_cycle_pot['Conductance (S)'].iloc[0]
        gain_dep = (df_cycle_dep['Conductance (S)'].iloc[-1]-df_cycle_dep['Conductance (S)'].iloc[0])/df_cycle_dep['Conductance (S)'].iloc[-1]
        #gain_pot = (df_cycle_pot['Conductance (S)'].iloc[-1] / df_cycle_pot['Conductance (S)'].iloc[0])
        #gain_dep = (df_cycle_dep['Conductance (S)'].iloc[-1] / df_cycle_dep['Conductance (S)'].iloc[0])
        #print(df_cycle_pot['Conductance (S)'].iloc[0])
        #print(df_cycle_pot.head(3))
        
        # Plot potentiation
        plt.plot(pulse_pot, G_norm_pot, 'o', label='Pot. Data')
        plt.plot(pulse_pot, G_fit_pot, '--', 
                label=f'Pot. Fit (R²={r2_pot:.2f}, Gain_p ={gain_pot:.2f}, NL_p={params_pot[1]:.2f} )')

        # Plot depression
        plt.plot(pulse_dep, G_norm_dep, 's', label='Dep. Data')
        plt.plot(pulse_dep, G_fit_dep, '--', 
                label=f'Dep. Fit (R²={r2_dep:.2f}, Gain_d ={gain_dep:.2f}, NL_p={params_dep[1]:.2f} )')
        
        plt.xlabel('Normalized Pulse')
        plt.ylabel('Normalized Conductance (S)')
        plt.title(title)
        plt.legend()
        plt.tight_layout()

        plt.show(block = False)
        plt.close('all')
        print('Plotting successful')
    except:
        print('Plotting failed')
        pass

def main(df,cell_number):
    # --- Main analysis loop ---

    #take only read data
    df_read = df[(abs(df['Voltage (V)']) < df.loc[5,'Value']+0.05)&(abs(df['Voltage (V)'] > df.loc[5,'Value']-0.05))]

    # process only last n cycles
    n = int(df_read['Cycle'].max()) 
    df_last_cycle = [None]*n
    for i in range(n):
        df_last_cycle[i] = df_read[df_read['Cycle']==(df_read['Cycle'].max()-i)]
    
    
    fitting_results = [] #prepare the fitting results

    for df_cycle in df_last_cycle:
        max_time = df_cycle['Time (s)'].max()
        min_time = df_cycle['Time (s)'].min()
        mid_time = (max_time + min_time) / 2

        # devide the dataframe into two: potentiation and depression
        df_cycle_pot = df_cycle[(df_cycle['Time (s)'] > min_time) & (df_cycle['Time (s)'] < mid_time)].copy()
        df_cycle_dep = df_cycle[(df_cycle['Time (s)'] > mid_time) & (df_cycle['Time (s)'] < max_time)].copy()

        # add column pulse
        df_cycle_pot['pulse'] = range(1, len(df_cycle_pot) + 1)
        df_cycle_dep['pulse'] = range(1, len(df_cycle_dep) + 1)

        #cut the first 10 datapoints for df_cycle_pot
        #df_cycle_pot = df_cycle_pot.iloc[10:]
        # Fit potentiation and depression
        pulse_pot, G_norm_pot, G_fit_pot, params_pot, r2_pot = fit_data_pot(df_cycle_pot, potentiation)
        pulse_dep, G_norm_dep, G_fit_dep, params_dep, r2_dep = fit_data_dep(df_cycle_dep, depression)

        # Plot combined
  
        plot_pot_dep(df_cycle_pot, pulse_pot, G_norm_pot, G_fit_pot, params_pot,r2_pot,
                    df_cycle_dep, pulse_dep, G_norm_dep, G_fit_dep, params_dep,r2_dep,
                    title=f'Cell {str(cell_number)} Cycle {df_cycle["Cycle"].iloc[0]}: Potentiation & Depression')

        # Store the results
        fitting_results.append({
            'Meas_Cycle': df_cycle['Cycle'].iloc[0],
            'Pot_S_init': df_cycle_pot['Conductance (S)'].iloc[0],
            'Pot_S_final': df_cycle_pot['Conductance (S)'].iloc[-1],
            'Pot_gain':(df_cycle_pot['Conductance (S)'].iloc[-1]-df_cycle_pot['Conductance (S)'].iloc[0])/df_cycle_pot['Conductance (S)'].iloc[0],
            'Pot_alpha': params_pot[0],
            'Pot_gamma': params_pot[1],
            'Pot_R2': r2_pot,
            'Dep_S_init': df_cycle_dep['Conductance (S)'].iloc[0],
            'Dep_S_final': df_cycle_dep['Conductance (S)'].iloc[-1],
            'Dep_gain':(df_cycle_dep['Conductance (S)'].iloc[-1]-df_cycle_dep['Conductance (S)'].iloc[0])/df_cycle_dep['Conductance (S)'].iloc[-1],
            'Dep_alpha': params_dep[0],
            'Dep_gamma': params_dep[1],
            'Dep_R2': r2_dep,
        })

  
    # --- Convert results to DataFrame ---
    fitting_results_df = pd.DataFrame(fitting_results)
    df_output_parameters_fitting_results = pd.concat([df, fitting_results_df], axis=1)
    #print("Fitting results:")
    #print(fitting_results_df)#
    return df_output_parameters_fitting_results


'''
from here bellow are functions to run BO
'''

def load_and_preprocess_data(file_path, variable_bounds):
    """
    Load and preprocess data for Bayesian optimization
    """
    # Load data
    df = pd.read_csv(file_path)
        
    # limit df only when the Pot_R2 more than 0.85
    #df = df[df["Pot_R2"] > 0.85]
    
    # Inputs and objectives
    X = df[["write_voltage", "pulse_duration", "t_pulse_to_pulse"]].values
    Y_gain = abs(df["Pot_gain"]).values.reshape(-1, 1)
    Y_gamma = abs(df["Pot_gamma"]/(1-abs(1-df["Pot_R2"]))).values.reshape(-1, 1)     # nonlinearity penaltized bu the R^2
    
    #Y_r2 = abs(df["Pot_R2"].values.reshape(-1,1))
    
    # Combine objectives: maximize gain, minimize abs(gamma)
    Y = np.hstack([Y_gain, -Y_gamma])  # maximize gain, minimize |gamma|, maximize R2
    
    # Convert to tensors
    X_tensor = torch.tensor(X, dtype=torch.float64)
    Y_tensor = torch.tensor(Y, dtype=torch.float64)

    
    # Normalize inputs based on provided bounds
    X_norm = normalize(X_tensor, bounds=variable_bounds)
    Y_norm = standardize(Y_tensor)
    
    return df, X_tensor, Y_tensor, X_norm, Y_norm


def train_gp_model(X_norm, Y_norm):
    """
    Train a multi-output GP model for multi-objective optimization
    """
    models = []
    for i in range(Y_norm.shape[1]):
        models.append(SingleTaskGP(X_norm, Y_norm[:, i:i+1]))
    
    model = ModelListGP(*models)
    mll = SumMarginalLogLikelihood(model.likelihood, model)
    fit_gpytorch_mll(mll)
    model.eval()
    
    return model


def optimize_acquisition_function(model, Y_norm, bounds):
    """
    Optimize the acquisition function to find the next candidate point
    """
    # Define reference point
    ref_point = torch.tensor([Y_norm[:, 0].min().item() - 0.1, 
                              Y_norm[:, 1].min().item() - 0.1
                              #Y_norm[:, 2].min().item() - 0.1
                              ], dtype=torch.float64)
    
    # Create partitioning
    partitioning = NondominatedPartitioning(ref_point=ref_point, Y=Y_norm)
    
    # Define acquisition function
    acq_func = qExpectedHypervolumeImprovement(
        model=model,
        ref_point=ref_point,
        partitioning=partitioning,
    )
    
    # Optimize acquisition function
    candidate, acq_value = optimize_acqf(
        acq_function=acq_func,
        bounds=bounds,
        q=1,
        num_restarts=5,
        raw_samples=20,
    )
    
    return candidate, acq_value, ref_point


def predict_candidate_objectives(model, candidate):
    """
    Get predicted objective values for the candidate
    """
    with torch.no_grad():
        pred = model.posterior(candidate).mean
    return pred


def is_pareto_efficient(costs):
    """
    Find the Pareto-efficient points
    """
    is_efficient = np.ones(costs.shape[0], dtype=bool)
    for i, c in enumerate(costs):
        if is_efficient[i]:
            is_efficient[is_efficient] = np.any(costs[is_efficient] > c, axis=1) | (costs[is_efficient] == c).all(axis=1)
    return is_efficient


def plot_normalized_objectives(Y_norm, ref_point, candidate_pred=None, show_hypervolume=True):
    """
    Plot the normalized objectives with Pareto front and candidate
    """
    plt.figure(figsize=(10, 8))
    
    # Plot normalized objectives
    Y_np = Y_norm.numpy()
    plt.scatter(Y_np[:, 0], Y_np[:, 1], c='blue', s=50, alpha=0.7, label='Existing Data')
    
    # Add labels and title
    plt.xlabel('Normalized Pot_gain (Higher is Better)', fontsize=12)
    plt.ylabel('Normalized -|Pot_gamma| (Higher is Better)', fontsize=12)
    plt.title('Normalized Objectives Space', fontsize=14)
    plt.grid(True, linestyle='--', alpha=0.7)
    
    # Find and highlight Pareto optimal points
    pareto_mask = is_pareto_efficient(Y_np)
    plt.scatter(Y_np[pareto_mask, 0], Y_np[pareto_mask, 1], c='red', s=100, 
                edgecolors='black', label='Pareto Front')
    
    # Add reference point
    plt.scatter([ref_point[0].item()], [ref_point[1].item()], c='green', s=150, 
                marker='*', label='Reference Point')
    
    # Add the new candidate point if provided
    if candidate_pred is not None:
        plt.scatter(candidate_pred[0, 0].item(), candidate_pred[0, 1].item(), 
                    c='purple', s=200, marker='X', label='New Candidate (Predicted)')
    
    # Add annotations for hypervolume
    if show_hypervolume:
        plt.axvline(x=ref_point[0].item(), color='green', linestyle='--', alpha=0.5)
        plt.axhline(y=ref_point[1].item(), color='green', linestyle='--', alpha=0.5)
        
        '''
        # Fill the hypervolume area for existing Pareto front
        for i in range(len(Y_np)):
            if pareto_mask[i]:
                plt.fill_between([ref_point[0].item(), Y_np[i, 0]], 
                                [ref_point[1].item(), ref_point[1].item()], 
                                [ref_point[1].item(), Y_np[i, 1]], 
                                alpha=0.1, color='blue')
        '''
    
    plt.legend(fontsize=12)
    plt.tight_layout()
    return plt


def plot_original_objectives(Y_gain, Y_gamma, Y_tensor, candidate_pred=None):
    """
    Plot objectives in original scale with Pareto front and candidate
    """
    plt.figure(figsize=(10, 8))
    
    # Get original objectives
    Y_orig = np.hstack([Y_gain, -Y_gamma])
    
    plt.scatter(Y_gain, -Y_gamma, c='blue', s=50, alpha=0.7, label='Existing Data')
    plt.xlabel('Pot_gain (Higher is Better)', fontsize=12)
    plt.ylabel('-|Pot_gamma| (Higher is Better)', fontsize=12)
    plt.title('Original Objectives Space', fontsize=14)
    plt.grid(True, linestyle='--', alpha=0.7)
    
    # Highlight Pareto front points in original space
    pareto_mask_orig = is_pareto_efficient(Y_orig)
    plt.scatter(Y_gain[pareto_mask_orig], -Y_gamma[pareto_mask_orig], 
                c='red', s=100, edgecolors='black', label='Pareto Front')
    
    # Add the new candidate point in original scale if provided
    if candidate_pred is not None:
        # Convert from normalized to original scale
        Y_mean = Y_tensor.mean(dim=0)
        Y_std = Y_tensor.std(dim=0)
        pred_original = candidate_pred * Y_std + Y_mean
        
        plt.scatter(pred_original[0, 0].item(), pred_original[0, 1].item(), 
                    c='purple', s=200, marker='X', label='New Candidate (Predicted)')
    
    plt.legend(fontsize=12)
    plt.tight_layout()
    return plt


def plot_parameters_vs_objectives(X, Y_gain, Y_gamma, original_candidate=None, pred_original=None):
    """
    Plot each input parameter against the objectives
    """
    fig, axs = plt.subplots(1, 3, figsize=(18, 6))
    
    # For each input parameter
    for i, param_name in enumerate(["write_voltage", "pulse_duration", "t_pulse_to_pulse"]):
        sc = axs[i].scatter(X[:, i], Y_gain, c=-Y_gamma, cmap='viridis', s=80, alpha=0.7)
        axs[i].set_xlabel(param_name, fontsize=12)
        axs[i].set_ylabel('Pot_gain', fontsize=12)
        axs[i].set_title(f'{param_name} vs. Objectives', fontsize=14)
        axs[i].grid(True, linestyle='--', alpha=0.7)

        # Add vertical line and point for candidate if provided
        if original_candidate is not None and pred_original is not None:
            axs[i].axvline(x=original_candidate.squeeze()[i].item(), color='red', linestyle='--')
            axs[i].scatter([original_candidate.squeeze()[i].item()], [pred_original[0, 0].item()], 
                           c='red', s=150, marker='X')

    # Adjust subplots to make room for the colorbar
    plt.subplots_adjust(right=0.85)  # Shrink the plot area to make space on the right

    # Add colorbar in a new axis outside the original axes
    cbar_ax = fig.add_axes([0.88, 0.15, 0.02, 0.7])  # [left, bottom, width, height]
    cbar = fig.colorbar(sc, cax=cbar_ax)
    cbar.set_label('-|Pot_gamma| (Higher is Better)', fontsize=12)

    return plt

def write_new_parameters(write_voltage, pulse_duration, t_pulse_to_pulse):
    # Function to write new parameters for the next measurement
    df_pot_dep_param = pd.read_csv(_param_file('parameter_Keysight_Potent_Depress_2.csv'))
    df_pot_dep_param.at[1, 'Value'] = write_voltage #write_voltage
    df_pot_dep_param.at[2, 'Value'] = -1*write_voltage #erase_voltage
    df_pot_dep_param.at[3, 'Value'] = pulse_duration #pulse_duration
    df_pot_dep_param.at[12, 'Value'] = t_pulse_to_pulse
    
    #save the new parameters back to this file
    df_pot_dep_param.to_csv(_param_file('parameter_Keysight_Potent_Depress_2.csv'), index=False)
    print('New parameters are written to Parameters/parameter_Keysight_Potent_Depress_2.csv')



def main_BO(cell_number):
    '''
    Main function for running BO
    '''
    #read the csv file
    folder_BO = _bo_dir()
    file_name = f'{cell_number}_potentiation_depression_summary.csv'
    
    file_path = (folder_BO+file_name)
    df = pd.read_csv(folder_BO+file_name)


    # Define explicit bounds for variables
    variable_bounds = torch.tensor([
        [1.1, 0.05, 0.15],  # Lower bounds for write_voltage, pulse_duration, and t_pulse_to_pulse
        [2.5, 0.5, 0.5]     # Upper bounds
    ], dtype=torch.float64)
    
    # Load and preprocess data
    df, X_tensor, Y_tensor, X_norm, Y_norm = load_and_preprocess_data(file_path, variable_bounds)
    
    # Extract original objectives for plotting
    Y_gain = abs(df["Pot_gain"]).values.reshape(-1, 1)
    Y_gamma = abs(df["Pot_gamma"]/(1-abs(1-df["Pot_R2"]))).values.reshape(-1, 1)
    #Y_gamma = abs(df["Pot_gamma"]).values.reshape(-1, 1)
    #Y_R2 = df["Pot_R2"].values.reshape(-1,1)
    
    # Train GP model
    model = train_gp_model(X_norm, Y_norm)
    
    # Define bounds for optimization (unit cube [0,1] for normalized space)
    opt_bounds = torch.stack([torch.zeros(3, dtype=torch.float64), torch.ones(3, dtype=torch.float64)])
    
    # Optimize acquisition function to get next candidate
    candidate, acq_value, ref_point = optimize_acquisition_function(model, Y_norm, opt_bounds)
    
    # Get predicted objective values for the candidate
    pred = predict_candidate_objectives(model, candidate)
    
    # Unnormalize candidate to get original point
    original_candidate = unnormalize(candidate, bounds=variable_bounds)

    write_voltage = round(float(original_candidate.squeeze()[0].item()), 2)
    pulse_duration = round(float(original_candidate.squeeze()[1].item()), 2)
    t_pulse_to_pulse = round(float(original_candidate.squeeze()[2].item()), 2)
    
      
    # Print results
    print("\nBayesian Optimization Results:")
    print("-" * 50)
    print("Recommended Next Parameters to Try:")
    print(f"write_voltage = {original_candidate.squeeze()[0].item():.2f}")
    print(f"pulse_duration = {original_candidate.squeeze()[1].item():.2f}")
    print(f"t_pulse_to_pulse = {original_candidate.squeeze()[2].item():.2f}")
    print(f"Acquisition value: {acq_value.item():.6f}")
    
    # Convert prediction to original scale
    Y_mean = Y_tensor.mean(dim=0)
    Y_std = Y_tensor.std(dim=0)
    pred_original = pred * Y_std + Y_mean
    
    print("\nPredicted Objective Values:")
    print(f"Pot_gain: {pred_original[0, 0].item():.3f}")
    print(f"Pot_gamma: {-pred_original[0, 1].item():.3f}")  # Negative since we stored -|gamma|
    #print(f"Pot_R2: {pred_original[0, 2].item():.3f}")
    print("-" * 50)
    
    #(re)write the recomended parameter to the measurement parameter file
    write_new_parameters(write_voltage, pulse_duration, t_pulse_to_pulse)
    
    try: 
        # Create and display plots
        plot1 = plot_normalized_objectives(Y_norm, ref_point, pred)
        plot1.show(block = False)
        plot1.close('all')

        plot2 = plot_original_objectives(Y_gain, Y_gamma, Y_tensor, pred)
        plot2.show(block = False)
        plot2.close('all')
        
        plot3 = plot_parameters_vs_objectives(X_tensor.numpy(), Y_gain, Y_gamma, 
                                            original_candidate, pred_original)
        plot3.show(block = False)
        plot3.close('all')
    except:
        print('Plotting failed')
        pass
    
    return 0 #model, candidate, original_candidate, pred, X_norm, Y_norm, variable_bounds


#main_BO(20)