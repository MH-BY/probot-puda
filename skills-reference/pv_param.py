"""Photovoltaic J-V parameter extraction (Voc, Jsc, FF, PCE, Rshunt, Rseries).

Ported verbatim from the original ``PV_param_calculation.py``. The original's
module-level test read was inside a commented (triple-quoted) block and has been
dropped here so the module is import-safe.
"""

import pandas as pd
import numpy as np


def interpolate_x_at_y0(x, y):
    """
    Given arrays x and y, where y crosses 0 somewhere,
    find the x-value at which y = 0 by simple linear interpolation.
    Assumes monotonic or single crossing in y.
    """
    # We look for consecutive points where y changes sign
    sign_changes = np.where(np.diff(np.sign(y)) != 0)[0]
    if len(sign_changes) == 0:
        return None  # No zero-crossing found
    idx = sign_changes[0]
    x1, x2 = x[idx], x[idx+1]
    y1, y2 = y[idx], y[idx+1]
    # Linear interpolation:
    return x1 + (x2 - x1) * (0.0 - y1) / (y2 - y1)


def interpolate_y_at_x0(x, y):
    """
    Interpolate to find y-value at x=0.
    Assumes x crosses 0 somewhere in the data.
    """
    sign_changes = np.where(np.diff(np.sign(x)) != 0)[0]
    if len(sign_changes) == 0:
        return None
    idx = sign_changes[0]
    x1, x2 = x[idx], x[idx+1]
    y1, y2 = y[idx], y[idx+1]
    # Linear interpolation for y at x=0
    return y1 + (y2 - y1)*(0.0 - x1)/(x2 - x1)


def linear_fit(x_sub, y_sub):
    """
    Returns slope, intercept of a best-fit line y = m*x + b
    """
    A = np.vstack([x_sub, np.ones(len(x_sub))]).T
    m, b = np.linalg.lstsq(A, y_sub, rcond=None)[0]
    return m, b


def calculate_parameters(df):
    cell_area_cm2 = float(df['Value'].iloc[5])
    irradiance_mW_per_cm2 = float(df['Value'].iloc[6])

    # Convert columns to NumPy arrays for convenience
    voltage = df["Voltage (V)"].values

    current_density = df["Current Density (mA/cm2)"].values  # mA/cm^2

    #------------------------------
    # Find Voc (voltage at which J=0)
    #-------------------------------
    Voc = interpolate_x_at_y0(voltage, current_density)

    #----------------------------------------
    # Find Jsc (current density at which V=0)
    #----------------------------------------
    Jsc = interpolate_y_at_x0(voltage, current_density)

    #-------------------------------
    #calculate FF
    #-------------------------------
    power_density = voltage * current_density  # (V) * (mA/cm^2) -> mW/cm^2
    idx_mpp = np.argmax(power_density)  # index of maximum power
    V_mpp = voltage[idx_mpp]
    J_mpp = current_density[idx_mpp]
    if (Voc is not None) and (Jsc is not None) and (Voc != 0) and (Jsc != 0):
        FF = (V_mpp * J_mpp)*100 / (Voc * Jsc)
    else:
        FF = None

    #--------------------
    # Calculate PCE
    #--------------------
    # PCE (in %) = (Pmax / Pin) * 100
    #   where Pin = 100 mW/cm^2 typically for 1 sun
    Pin = irradiance_mW_per_cm2*100  # 100 mW/cm^2
    P_mpp = power_density[idx_mpp]  # mW/cm^2
    PCE = (P_mpp / Pin) * 100.0

    #-------------------------------------
    # Calculate the Rshunt. dV/dI near V=0
    #-------------------------------------
    current_mA = current_density * cell_area_cm2 #need this because we need to calculate Resistance
    # find points near 0 V (for instance, within +/- 0.05 V)
    V_window = 0.1
    mask_rsh = (voltage > -V_window) & (voltage < V_window)
    if sum(mask_rsh) >= 2:
        m_rsh, b_rsh = linear_fit(voltage[mask_rsh],current_mA[mask_rsh]/1000)
        #slope = dV/dI, so that is effectively R = slope
        Rshunt = abs(1/m_rsh)
    else:
        Rshunt = None

    #-------------------------------------
    # Calculate the Rseries. dV/dI near J=0, or near Voc
    #-------------------------------------
    # find points near Voc (within +/- 0.05 V for example)
    if Voc is not None:
        mask_rs = (voltage > Voc - V_window) & (voltage < Voc + V_window)
        if sum(mask_rs) >= 2:
            m_rs, b_rs = linear_fit(voltage[mask_rs],current_mA[mask_rs]/1000)
            Rseries = abs(1/m_rs)
        else:
            Rseries = None
    else:
        Rseries = None

    df_calculated_params = pd.DataFrame({
        'PV_params': ['PCE (%)','FF (%)','Voc (V)','Jsc (mA/cm2)','Rshunt (Ohm)','Rseries (Ohm)'],
        'PV_param_values':[PCE,FF,Voc,Jsc,Rshunt,Rseries],
    })
    print(df_calculated_params)
    return df_calculated_params
