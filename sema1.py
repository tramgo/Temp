import os
import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd
# Removed yfinance import
from ta import trend, momentum, volatility, volume
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.backends.backend_pdf import PdfPages
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.env_checker import check_env
from stable_baselines3.common.callbacks import CheckpointCallback, BaseCallback, CallbackList
import torch
import warnings
from typing import Optional, Tuple
import random
import datetime
from sklearn.preprocessing import StandardScaler
import math
import logging
from pathlib import Path
import optuna
import joblib
import time
import plotly.io as pio

# Import ConcurrentRotatingFileHandler for robust multi-process logging
try:
    from concurrent_log_handler import ConcurrentRotatingFileHandler
except ImportError:
    raise ImportError("Please install 'concurrent-log-handler' package via pip: pip install concurrent-log-handler")
# Suppress warnings for cleaner output
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# Set random seeds for reproducibility
RANDOM_SEED = 42
random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)
torch.manual_seed(RANDOM_SEED)

# Define feature sets as constants
FEATURES_TO_SCALE = [
    'Close', 'SMA10', 'SMA50', 'RSI', 'MACD', 'ADX',  # Added 'ADX'
    'BB_Upper', 'BB_Lower', 'Bollinger_Width',
    'EMA20', 'VWAP', 'Lagged_Return', 'Volatility'
]

UNSCALED_FEATURES = [
    f"{feature}_unscaled" for feature in FEATURES_TO_SCALE
]

# Define directories for results and plots
BASE_DIR = Path('.').resolve()
RESULTS_DIR = BASE_DIR / 'results'
PLOTS_DIR = BASE_DIR / 'plots'
TB_LOG_DIR = BASE_DIR / 'tensorboard_logs'
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
PLOTS_DIR.mkdir(parents=True, exist_ok=True)
TB_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Function to set up separate loggers
def setup_logger(name: str, log_file: Path, level=logging.INFO) -> logging.Logger:
    """
    Sets up a logger with the specified name and log file.

    Args:
        name (str): Name of the logger.
        log_file (Path): Path to the log file.
        level (int, optional): Logging level. Defaults to logging.INFO.

    Returns:
        logging.Logger: Configured logger.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Prevent adding multiple handlers to the logger
    if not logger.handlers:
        handler = ConcurrentRotatingFileHandler(str(log_file), maxBytes=10**6, backupCount=5, encoding='utf-8')
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

# Initialize separate loggers
main_logger = setup_logger('main_logger', RESULTS_DIR / 'main.log', level=logging.DEBUG)
training_logger = setup_logger('training_logger', RESULTS_DIR / 'training.log', level=logging.DEBUG)
testing_logger = setup_logger('testing_logger', RESULTS_DIR / 'testing.log', level=logging.DEBUG)
phase_logger = setup_logger('phase_logger', RESULTS_DIR / 'phase.log', level=logging.INFO)

# Log the absolute paths
main_logger.info(f"Base Directory: {BASE_DIR}")
main_logger.info(f"Results Directory: {RESULTS_DIR}")
main_logger.info(f"Plots Directory: {PLOTS_DIR}")
main_logger.info(f"TensorBoard Logs Directory: {TB_LOG_DIR}")

# Function to log phase indicators
def log_phase(phase: str, status: str = "Starting", env_details: dict = None, duration: float = None):
    """
    Logs the current phase of the program.

    Args:
        phase (str): The name of the phase (e.g., 'Hyperparameter Tuning').
        status (str, optional): The status of the phase (e.g., 'Starting', 'Completed'). Defaults to "Starting".
        env_details (dict, optional): Key-value pairs describing environment details.
        duration (float, optional): Duration of the phase in seconds.
    """
    log_message = f"***** {status} {phase} *****"
    if env_details:
        log_message += f"\nEnvironment Details: {env_details}"
    if duration is not None:
        log_message += f"\nDuration: {duration:.2f} seconds ({duration/60:.2f} minutes)"
    phase_logger.info(log_message)

# Configure Logging for Main Logger
main_logger.info("Logging has been configured with separate loggers for main, training, testing, and phases.")
##############################################
# Version Checks
##############################################

def check_versions():
    """
    Checks and logs the versions of key libraries to ensure compatibility.
    """
    import stable_baselines3
    import gymnasium
    import optuna

    sb3_version = stable_baselines3.__version__
    gymnasium_version = gymnasium.__version__
    optuna_version = optuna.__version__

    main_logger.debug(f"Stable Baselines3 version: {sb3_version}")
    main_logger.debug(f"Gymnasium version: {gymnasium_version}")
    main_logger.debug(f"Optuna version: {optuna_version}")

    # Ensure SB3 is at least version 2.0.0 for Gymnasium support
    try:
        sb3_major, sb3_minor, sb3_patch = map(int, sb3_version.split('.')[:3])
        if sb3_major < 2:
            main_logger.error("Stable Baselines3 version must be at least 2.0.0. Please upgrade SB3.")
            exit()
    except:
        main_logger.error("Unable to parse Stable Baselines3 version. Please ensure it's installed correctly.")
        exit()

    # Ensure Gymnasium is updated
    if gymnasium_version < '0.28.1':  # Example minimum version
        main_logger.warning("Consider upgrading Gymnasium to the latest version for better compatibility.")

check_versions()
##############################################
# Fetch and Prepare Data
##############################################

def get_data(csv_file_path: str, scaler: Optional[StandardScaler] = None, fit_scaler: bool = False) -> Tuple[pd.DataFrame, Optional[StandardScaler]]:
    """
    Reads historical stock data from a CSV file, calculates technical indicators,
    and performs scaling on the features.

    Args:
        csv_file_path (str): Path to the CSV file containing stock data.
        scaler (Optional[StandardScaler], optional): Scaler object. Defaults to None.
        fit_scaler (bool, optional): Whether to fit the scaler on the data. Defaults to False.

    Returns:
        Tuple[pd.DataFrame, Optional[StandardScaler]]: Processed DataFrame with technical indicators and scaled features, and the scaler.
    """
    main_logger.info(f"Reading data from CSV file at {csv_file_path}")

    # Read data from CSV
    try:
        df = pd.read_csv(csv_file_path)
    except FileNotFoundError:
        main_logger.error(f"CSV file not found at {csv_file_path}")
        return pd.DataFrame(), scaler
    except Exception as e:
        main_logger.error(f"Error reading CSV file: {e}")
        return pd.DataFrame(), scaler

    if df.empty:
        main_logger.error(f"No data found in CSV file at {csv_file_path}")
        # Save empty DataFrame to CSV
        empty_file = RESULTS_DIR / "data_fetched_empty.csv"
        df.to_csv(empty_file, index=True)
        main_logger.info(f"Empty fetched data saved to {empty_file}")
        return df, scaler

    # Ensure the Date column is parsed correctly
    if 'Date' not in df.columns:
        main_logger.error("CSV file must contain a 'Date' column.")
        return pd.DataFrame(), scaler

    df['Date'] = pd.to_datetime(df['Date'])
    df.sort_values('Date', inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Select specific columns and ensure they are of the correct type
    required_columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
    for col in required_columns:
        if col not in df.columns:
            main_logger.error(f"Missing required column '{col}' in CSV file.")
            # Save DataFrame with missing columns
            error_file = RESULTS_DIR / f"data_error_missing_column_{col}.csv"
            df.to_csv(error_file, index=True)
            main_logger.info(f"Data with missing column {col} saved to {error_file}")
            return pd.DataFrame(), scaler

    # Save fetched data immediately after reading
    fetched_data_file = RESULTS_DIR / "data_fetched.csv"
    df.to_csv(fetched_data_file, index=True)
    main_logger.info(f"Fetched data saved to {fetched_data_file}")

    # Convert columns to numeric, coercing errors
    numeric_cols = ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce')

    # Check for sufficient data
    if len(df) < 200:
        main_logger.error("Not enough data points in CSV file.")
        # Save insufficient data DataFrame
        insufficient_file = RESULTS_DIR / "data_insufficient.csv"
        df.to_csv(insufficient_file, index=True)
        main_logger.info(f"Insufficient data saved to {insufficient_file}")
        return pd.DataFrame(), scaler

    close_col = 'Close'
    try:
        close = df[close_col].squeeze()
        high = df['High'].squeeze()
        low = df['Low'].squeeze()
        volume_col = df['Volume'].squeeze()

        # Calculate technical indicators
        sma10 = trend.SMAIndicator(close=close, window=10).sma_indicator()
        sma50 = trend.SMAIndicator(close=close, window=50).sma_indicator()
        rsi = momentum.RSIIndicator(close=close, window=14).rsi()
        macd = trend.MACD(close=close).macd()
        adx = trend.ADXIndicator(high=high, low=low, close=close, window=14).adx()
        bollinger = volatility.BollingerBands(close=close, window=20, window_dev=2)
        bb_upper = bollinger.bollinger_hband()
        bb_lower = bollinger.bollinger_lband()
        bollinger_width = bollinger.bollinger_wband()
        ema20 = trend.EMAIndicator(close=close, window=20).ema_indicator()
        vwap = volume.VolumeWeightedAveragePrice(high=high, low=low, close=close, volume=volume_col, window=14).volume_weighted_average_price()
        lagged_return = close.pct_change().fillna(0)
        atr = volatility.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()

        # Verify that all indicators were calculated successfully
        indicators = {
            'SMA10': sma10,
            'SMA50': sma50,
            'RSI': rsi,
            'MACD': macd,
            'ADX': adx,
            'BB_Upper': bb_upper,
            'BB_Lower': bb_lower,
            'Bollinger_Width': bollinger_width,
            'EMA20': ema20,
            'VWAP': vwap,
            'Lagged_Return': lagged_return,
            'Volatility': atr
        }

        for key, value in indicators.items():
            if value.isnull().all():
                main_logger.error(f"Technical indicator {key} could not be calculated properly.")
                # Save DataFrame with failed indicator
                failed_indicator_file = RESULTS_DIR / f"data_failed_indicator_{key}.csv"
                df.to_csv(failed_indicator_file, index=True)
                main_logger.info(f"Data with failed indicator {key} saved to {failed_indicator_file}")
                return pd.DataFrame(), scaler

    except Exception as e:
        main_logger.error(f"Error calculating indicators: {e}")
        # Save DataFrame with error
        error_calculation_file = RESULTS_DIR / "data_error_calculation.csv"
        df.to_csv(error_calculation_file, index=True)
        main_logger.info(f"Data with calculation error saved to {error_calculation_file}")
        return pd.DataFrame(), scaler

    # Append indicators to DataFrame
    for key, value in indicators.items():
        df[key] = value

    # Save DataFrame after adding indicators (before scaling)
    df_before_scaling_file = RESULTS_DIR / "data_before_scaling.csv"
    df.to_csv(df_before_scaling_file, index=True)
    main_logger.info(f"Data with indicators saved before scaling to {df_before_scaling_file}")

    # Add unscaled versions of relevant features
    for feature in FEATURES_TO_SCALE:
        if feature in df.columns:
            df[f"{feature}_unscaled"] = df[feature].copy()  # Use copy to ensure separation
            main_logger.debug(f"Added column: {feature}_unscaled")
        else:
            main_logger.error(f"Feature {feature} is missing from DataFrame. Cannot create {feature}_unscaled.")
            # Save DataFrame with missing feature
            missing_feature_file = RESULTS_DIR / f"data_missing_feature_{feature}.csv"
            df.to_csv(missing_feature_file, index=True)
            main_logger.info(f"Data with missing feature {feature} saved to {missing_feature_file}")
            return pd.DataFrame(), scaler

    # Save DataFrame after adding unscaled columns
    df_after_unscaled_file = RESULTS_DIR / "data_after_unscaled.csv"
    df.to_csv(df_after_unscaled_file, index=True)
    main_logger.info(f"Data with unscaled features saved to {df_after_unscaled_file}")

    # Handle missing values
    df.fillna(method='ffill', inplace=True)
    df.fillna(0, inplace=True)
    df.reset_index(inplace=True)

    # Data Validation: Check for columns filled with zeros
    zero_filled_columns = df[required_columns].columns[(df[required_columns] == 0).all()].tolist()
    if zero_filled_columns:
        main_logger.error(f"One or more required columns are entirely filled with zeros: {zero_filled_columns}. Aborting data processing.")
        # Save DataFrame with zero-filled columns
        zero_filled_file = RESULTS_DIR / "data_zero_filled_columns.csv"
        df.to_csv(zero_filled_file, index=True)
        main_logger.info(f"Data with zero-filled columns saved to {zero_filled_file}")
        return pd.DataFrame(), scaler

    main_logger.info("Data fetched and processed successfully from CSV.")

    # Scaling Features
    if fit_scaler:
        scaler = StandardScaler()
        df[FEATURES_TO_SCALE] = scaler.fit_transform(df[FEATURES_TO_SCALE])
        main_logger.debug("Features scaled and scaler fitted.")
    elif scaler is not None:
        df[FEATURES_TO_SCALE] = scaler.transform(df[FEATURES_TO_SCALE])
        main_logger.debug("Features scaled using existing scaler.")
    else:
        main_logger.error("Scaler not provided for scaling. Returning unscaled data.")
        # Save unscaled DataFrame
        unscaled_file = RESULTS_DIR / "data_unscaled_final.csv"
        df.to_csv(unscaled_file, index=True)
        main_logger.info(f"Unscaled data saved to {unscaled_file}")
        return df, scaler

    # Save DataFrame after scaling
    df_scaled_file = RESULTS_DIR / "data_scaled.csv"
    df.to_csv(df_scaled_file, index=True)
    main_logger.info(f"Scaled data saved to {df_scaled_file}")

    return df, scaler
##############################################
# Custom Trading Environment
##############################################

class SingleStockTradingEnv(gym.Env):
    """
    A custom Gym environment for single stock trading with continuous action space.
    """
    metadata = {'render.modes': ['human']}

    def __init__(self, df: pd.DataFrame, scaler: StandardScaler,
                 initial_balance: float = 100000,
                 stop_loss: float = 0.90, take_profit: float = 1.10,
                 max_position_size: float = 0.5, max_drawdown: float = 0.20,
                 annual_trading_days: int = 252, transaction_cost: float = 0.0001,
                 env_rank: int = 0,
                 some_factor: float = 0.01,  # Default or override
                 hold_threshold: float = 0.1, 
                 reward_weights: Optional[dict] = None,

                # --- NEWLY ADDED ARGS FOR TRAILING STOP ---
                trailing_drawdown_trigger: float = 0.20,   # e.g. 20% from peak
                trailing_drawdown_grace: int = 3,          # how many consecutive steps
                forced_liquidation_penalty: float = -5.0):  # extra penalty on forced sell
        super(SingleStockTradingEnv, self).__init__()

        # Store new factor
        self.some_factor = some_factor

        self.env_rank = env_rank
        self.df = df.copy().reset_index(drop=True)
        self.scaler = scaler
        self.initial_balance = initial_balance
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.max_position_size = max_position_size
        self.max_drawdown = max_drawdown
        self.annual_trading_days = annual_trading_days
        self.transaction_cost = transaction_cost  # 0.1% per trade
        self.hold_threshold = hold_threshold
        self.consecutive_drawdown_steps = 0
        # self.ema_alpha = 0
        self.reward_var_ema = 0
        import collections
        self.reward_history = collections.deque(maxlen=500)
        
        # --- STORE NEW TRAILING STOP PARAMETERS ---
        self.trailing_drawdown_trigger = trailing_drawdown_trigger
        self.trailing_drawdown_grace = trailing_drawdown_grace
        self.forced_liquidation_penalty = forced_liquidation_penalty
    
        # Action space: Continuous actions between -1 and 1
        # Negative values: Sell proportion of holdings
        # Positive values: Buy proportion of available balance
        # Zero: Hold
        self.action_space = spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32)

        # Observation space: features + balance, net worth, position + market phase
        self.num_features = len(FEATURES_TO_SCALE)
        self.market_phase = ['Bull', 'Bear', 'Sideways']
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(self.num_features + 3 + len(self.market_phase) + 2,),
            dtype=np.float32
        )

        self.feature_names = FEATURES_TO_SCALE

        # Initialize environment state
        self.reset()

        # Initialize reward weights
        if reward_weights is not None:
            self.reward_weights = reward_weights
        else:
            self.reward_weights = {'reward_scale': 1.0}  # Renamed to prevent confusion

        training_logger.debug(f"[Env {self.env_rank}] Initialized with reward_weights: {self.reward_weights}")

    def seed(self, seed=None):
        """
        Sets the seed for the environment's random number generators.

        Args:
            seed (int, optional): Seed value. Defaults to None.
        """
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        training_logger.debug(f"[Env {self.env_rank}] Seed set to {seed}")

    def _next_observation(self) -> np.ndarray:
        """
        Constructs and returns the current observation for the agent,
        including:
          1) The existing technical features
          2) Scaled balance/net_worth/position
          3) One-hot market phase
          4) current_drawdown_fraction
          5) drawdown_buffer
        """

        # (A) Ensure we don't go out of bounds
        if self.current_step >= len(self.df):
            self.current_step = len(self.df) - 1

        # (B) Get the row/data at current_step
        current_data = self.df.iloc[self.current_step]

        # (C) Extract the technical feature array
        #    Suppose self.feature_names = list of your scaled columns
        features = current_data[self.feature_names].values  # shape = (num_features,)

        # Build a list to accumulate obs
        obs = list(features)  # now you have your num_features indicator columns

        # (D) Add scaled balance, net worth, position
        obs.append(self.balance / self.initial_balance)
        obs.append(self.net_worth / self.initial_balance)
        obs.append(self.position / self.initial_balance)

        # (E) Market phase logic (existing approach)
        try:
            adx = float(current_data['ADX_unscaled'])
        except KeyError:
            self.training_logger.error(f"[Env {self.env_rank}] 'ADX_unscaled' not found at step {self.current_step}. Using 0.0")
            adx = 0.0

        if adx > 25:
            try:
                sma10 = float(current_data['SMA10_unscaled'])
                sma50 = float(current_data['SMA50_unscaled'])
                if sma10 > sma50:
                    phase = 'Bull'
                else:
                    phase = 'Bear'
            except KeyError as e:
                self.training_logger.error(f"[Env {self.env_rank}] Missing SMA columns: {e}. Setting phase=Sideways.")
                phase = 'Sideways'
        else:
            phase = 'Sideways'

        # One-hot encode: for p in self.market_phase = ['Bull','Bear','Sideways']
        for p in self.market_phase:
            obs.append(1.0 if phase == p else 0.0)

        # (F) current_drawdown_fraction
        #    We assume self.peak is updated each step => self.peak = max(self.peak, self.net_worth)
        if self.peak > 0:
            current_drawdown_fraction = (self.peak - self.net_worth) / self.peak
        else:
            current_drawdown_fraction = 0.0  # no drawdown if peak=0 or net_worth=0

        obs.append(current_drawdown_fraction)

        # (G) drawdown_buffer = meltdown_threshold - current_drawdown_fraction
        meltdown_threshold = self.max_drawdown  # e.g. 0.15
        drawdown_buffer = meltdown_threshold - current_drawdown_fraction
        if drawdown_buffer < 0.0:
            drawdown_buffer = 0.0

        obs.append(drawdown_buffer)

        # (H) Convert to np.array
        obs = np.array(obs, dtype=np.float32)

        # Replace any NaN or Inf with 0
        if np.isnan(obs).any() or np.isinf(obs).any():
            obs = np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)

        # (I) Sanity checks
        # E.g. your obs space shape = (self.num_features + 3 + len(self.market_phase) + 2,)
        expected_size = self.observation_space.shape[0]
        assert obs.shape[0] == expected_size, f"Observation shape mismatch: got {obs.shape[0]} vs {expected_size}"
        assert not np.isnan(obs).any(), "Observation still has NaN!"

        return obs


    def reset(self, seed: Optional[int] = None, options: Optional[dict] = None):
        """
        Resets the state of the environment to an initial state.

        Returns:
            Tuple[np.ndarray, dict]: The initial observation and an empty info dictionary.
        """
        super().reset(seed=seed)
        self.balance = self.initial_balance
        self.position = 0
        self.net_worth = self.initial_balance
        self.current_step = 0
        self.history = []
        self.prev_net_worth = self.net_worth
        self.last_action = 0.0  # Initialize last_action as Hold
        self.peak = self.net_worth
        self.returns_window = []
        self.transaction_count = 0  # Initialize transaction count
        self.consecutive_drawdown_steps = 0
        self.reward_history.clear()
        self.reward_ema = 0
        self.reward_var_ema = 0
        self.ema_alpha = 0
        self.reward_warmup_count = 0
        
        training_logger.debug(f"[Env {self.env_rank}] Environment reset.")
        return self._next_observation(), {}

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, float, bool, bool, dict]:
        # --- Logging and Action Validation ---
        training_logger.debug(f"[Env {self.env_rank}] step() called at current_step={self.current_step} with action={action}")
        try:
            action_value = float(action[0])
            assert self.action_space.contains(action), f"[Env {self.env_rank}] Invalid action: {action}"
        except Exception as e:
            training_logger.error(f"[Env {self.env_rank}] Action validation failed: {e}")
            return self._next_observation(), -1000.0, True, False, {}

        invalid_action_penalty = -0.01
        hold_threshold = self.hold_threshold
        # Uncomment below if you wish to treat near-zero actions as Hold:
        """ if abs(action_value) <= hold_threshold:
            action_value = 0.0
            training_logger.debug(f"[Env {self.env_rank}] Action within threshold; treated as Hold.") """

        # --- End-of-Data Guard ---
        if self.current_step >= len(self.df):
            terminated = True
            truncated = False
            reward = -1000.0
            obs = self._next_observation()
            self.history.append({
                'Date': self.df.iloc[self.current_step]['Date'],
                'Close_unscaled': self.df.iloc[self.current_step]['Close_unscaled'],
                'Action': np.nan,
                'Buy_Signal_Price': np.nan,
                'Sell_Signal_Price': np.nan,
                'Net Worth': self.net_worth,
                'Balance': self.balance,
                'Position': self.position,
                'Reward': reward,
                'Trade_Cost': 0.0
            })
            training_logger.error(f"[Env {self.env_rank}] Terminating episode at step {self.current_step} due to data overflow.")
            return obs, reward, terminated, truncated, {}

        # --- Get Current Data ---
        current_data = self.df.iloc[self.current_step]
        current_price = float(current_data['Close_unscaled'])
        current_date = current_data['Date']

        shares_traded = 0
        trade_cost = 0.0
        invalid_act_penalty = 0.0

        # --- Trading Logic ---
        if action_value > 0:
            # Buy logic
            investment_amount = self.balance * action_value * self.max_position_size
            shares_to_buy = math.floor(investment_amount / current_price)
            if shares_to_buy == 0:
                one_share_cost = current_price * (1 + self.transaction_cost)
                if one_share_cost <= self.balance:
                    shares_to_buy = 1
            total_cost = shares_to_buy * current_price * (1 + self.transaction_cost)
            if shares_to_buy > 0 and total_cost <= self.balance:
                self.balance -= total_cost
                self.position += shares_to_buy
                self.transaction_count += 1
                shares_traded = shares_to_buy
                trade_cost = shares_traded * current_price * self.transaction_cost
                training_logger.debug(f"[Env {self.env_rank}] Step {self.current_step}: Bought {shares_to_buy} shares at {current_price:.2f}")
            else:
                invalid_act_penalty = invalid_action_penalty
                training_logger.debug(f"[Env {self.env_rank}] Step {self.current_step}: Buy action invalid (insufficient balance or zero shares calculated).")
        elif action_value < 0:
            # Sell logic
            proportion_to_sell = abs(action_value) * self.max_position_size
            shares_to_sell = math.floor(self.position * proportion_to_sell)
            if shares_to_sell == 0 and self.position > 0:
                shares_to_sell = 1
            if shares_to_sell > 0 and shares_to_sell <= self.position:
                proceeds = shares_to_sell * current_price * (1 - self.transaction_cost)
                self.position -= shares_to_sell
                self.balance += proceeds
                self.transaction_count += 1
                shares_traded = shares_to_sell
                trade_cost = shares_traded * current_price * self.transaction_cost
                training_logger.debug(f"[Env {self.env_rank}] Step {self.current_step}: Sold {shares_to_sell} shares at {current_price:.2f}")
            else:
                invalid_act_penalty = invalid_action_penalty
                training_logger.debug(f"[Env {self.env_rank}] Step {self.current_step}: Sell action invalid (insufficient shares).")
        else:
            # Hold action
            training_logger.debug(f"[Env {self.env_rank}] Step {self.current_step}: Hold action received.")

        # --- Net Worth Calculation ---
        net_worth = float(self.balance + self.position * current_price)
        net_worth_change = net_worth - self.prev_net_worth

        # --- Forced Stop/Take Profit Penalties ---
        forced_stop_penalty = -3.0 if (net_worth <= self.initial_balance * self.stop_loss and self.position > 0) else 0.0
        forced_tp_penalty = -1.0 if (net_worth >= self.initial_balance * self.take_profit and self.position > 0) else 0.0

        # --- Profit Reward Calculation ---
        profit_weight = self.reward_weights.get('profit_weight', 1.5)
        profit_reward = (net_worth_change / self.initial_balance) * profit_weight

        # --- Sharpe Bonus Calculation via Returns Window ---
        step_return = net_worth_change / self.initial_balance
        self.returns_window.append(step_return)
        if len(self.returns_window) > 30:
            self.returns_window.pop(0)
        if len(self.returns_window) >= 10:
            mean_return = np.mean(self.returns_window)
            std_return = np.std(self.returns_window) + 1e-9
            sharpe = mean_return / std_return
            sharpe_bonus = sharpe * self.reward_weights.get('sharpe_bonus_weight', 0.05)
        else:
            sharpe_bonus = 0.0

        # --- Drawdown Penalty Calculation ---
        self.peak = max(self.peak, net_worth)
        current_drawdown = (self.peak - net_worth) / self.peak if self.peak > 0 else 0.0
        drawdown_penalty = 0.0
        if current_drawdown > 0.05:
            drawdown_penalty -= (2.0 + self.initial_balance * self.some_factor)
        if current_drawdown > 0.1:
            drawdown_penalty = -abs(drawdown_penalty) * 1.25
        # Partial forced liquidation if drawdown > 15%
        if current_drawdown > 0.15 and self.position > 0:
            shares_to_sell = math.floor(self.position * 0.5)
            if shares_to_sell > 0:
                proceeds = shares_to_sell * current_price * (1 - self.transaction_cost)
                self.balance += proceeds
                self.position -= shares_to_sell
                self.transaction_count += 1
                shares_traded = shares_to_sell
                trade_cost += shares_traded * current_price * self.transaction_cost
                self.peak = float(self.balance + self.position * current_price)
                training_logger.info(f"[Env {self.env_rank}] Partial forced liquidation at ~15% drawdown. Selling {shares_to_sell} shares...")
                # Reset drawdown counter and update prev_net_worth immediately
                self.consecutive_drawdown_steps = 0
                self.prev_net_worth = float(self.balance + self.position * current_price)
        # Full forced liquidation if drawdown > 20%
        if current_drawdown > 0.2 and self.position > 0:
            shares_to_sell = self.position
            if shares_to_sell > 0:
                proceeds = shares_to_sell * current_price * (1 - self.transaction_cost)
                self.balance += proceeds
                self.transaction_count += 1
                shares_traded = shares_to_sell
                trade_cost += shares_traded * current_price * self.transaction_cost
                self.position = 0
                self.peak = self.balance
                training_logger.info(f"[Env {self.env_rank}] Full forced liquidation at ~20% drawdown. Selling {shares_to_sell} shares...")
                # Reset drawdown counter and update prev_net_worth immediately
                self.consecutive_drawdown_steps = 0
                self.prev_net_worth = self.balance

        # Apply an additional multiplier to the drawdown penalty if needed
        drawdown_penalty = -abs(drawdown_penalty) * 1.25

        # Recalculate net_worth after any forced liquidations
        net_worth = float(self.balance + self.position * current_price)
        self.net_worth = net_worth

        # --- Holding Bonus Calculation ---
        hold_factor = max(0, 1 - abs(action_value) / 0.1)
        raw_vol = current_data['Volatility_unscaled']
        vol_thresh = self.reward_weights.get('volatility_threshold', 1.0)
        volatility_factor = 1.0 - np.clip(raw_vol / vol_thresh, 0.0, 1.0)
        mom_thresh_min = self.reward_weights.get('momentum_threshold_min', 30)
        mom_thresh_max = self.reward_weights.get('momentum_threshold_max', 70)
        if mom_thresh_max > mom_thresh_min:
            raw_rsi = current_data['RSI_unscaled']
            rsi_factor = (raw_rsi - mom_thresh_min) / (mom_thresh_max - mom_thresh_min)
            rsi_factor = np.clip(rsi_factor, 0.0, 1.0)
        else:
            rsi_factor = 0.0
        favorable_hold_factor = hold_factor * volatility_factor * rsi_factor
        holding_bonus_weight = self.reward_weights.get('holding_bonus_weight', 0.001)
        holding_bonus = favorable_hold_factor * holding_bonus_weight * net_worth

        # --- Transaction Penalty Calculation ---
        penalty_scale = self.reward_weights.get('transaction_penalty_scale', 1.0)
        transaction_penalty = -(trade_cost / self.initial_balance) * penalty_scale

        # --- Accumulate Raw Reward (All Components) ---
        raw_reward = (profit_reward + sharpe_bonus + forced_stop_penalty +
                      forced_tp_penalty + drawdown_penalty +
                      transaction_penalty + holding_bonus + invalid_act_penalty)

        # --- EMA-Based Reward Smoothing for Raw Reward ---
        # Initialize EMA variables if they don't exist
        # Initialize EMA variables if they don't exist
        if not hasattr(self, 'reward_ema'):
            self.reward_ema = 0.0
            self.reward_var_ema = 1e-6
            self.ema_alpha = self.reward_weights.get('ema_alpha', 0.01)  # Smoothing factor

        # self.ema_alpha = 0.182
        self.ema_alpha = self.reward_weights.get('ema_alpha', 0.01)  # Smoothing factor
        
        # ----- EMA-Based Reward Smoothing with Warmup and Two-Step Update -----
        # Set a warmup threshold (for example, 10 steps)
        warmup_steps = 10
        if not hasattr(self, 'reward_warmup_count'):
            self.reward_warmup_count = 0

        # Initialize EMA variables if they don't exist
        if not hasattr(self, 'reward_ema'):
            # Instead of starting at zero, initialize EMA with the current raw_reward to avoid huge initial deviations.
            self.reward_ema = raw_reward  
            self.reward_var_ema = 1e-6
            normalized_reward = raw_reward  # Use raw_reward during warmup
            self.reward_warmup_count += 1
        else:
            if self.reward_warmup_count < warmup_steps:
                # During warmup, simply use the raw reward for normalization (or a linear average)
                normalized_reward = raw_reward
                old_reward_ema = self.reward_ema
                old_reward_var_ema = self.reward_var_ema
                self.reward_ema = self.ema_alpha * raw_reward + (1 - self.ema_alpha) * old_reward_ema
                self.reward_var_ema = self.ema_alpha * ((raw_reward - old_reward_ema) ** 2) + (1 - self.ema_alpha) * old_reward_var_ema
                self.reward_warmup_count += 1
            else:
                # Normal phase: use the previous EMA values to compute normalized_reward
                old_reward_ema = self.reward_ema
                old_reward_var_ema = self.reward_var_ema
                normalized_reward = (raw_reward - old_reward_ema) / (np.sqrt(old_reward_var_ema) + 1e-8)
                # Now update the EMA estimates with the current raw_reward
                self.reward_ema = self.ema_alpha * raw_reward + (1 - self.ema_alpha) * old_reward_ema
                self.reward_var_ema = self.ema_alpha * ((raw_reward - old_reward_ema) ** 2) + (1 - self.ema_alpha) * old_reward_var_ema

            # Retrieve the tunable reward_norm_factor (to be tuned between, for example, 0.1 and 5.0)
            reward_norm_factor = self.reward_weights.get('reward_norm_factor', 1.0)
            # Adjust the normalized reward to prevent tanh saturation
            adjusted_reward = normalized_reward / reward_norm_factor

            # Apply tanh squashing to compress the value to the range (-1, 1)
            scaled_reward = np.tanh(adjusted_reward)

            # Finally, apply any additional global scaling
            final_reward = scaled_reward * self.reward_weights.get('reward_scale', 1.0)
            normalized_reward = final_reward

        # --- Append Step Details to History ---
        self.history.append({
            'Date': current_date,
            'Close_unscaled': current_price,
            'Action': action_value,
            'Buy_Signal_Price': current_price if action_value > 0 else np.nan,
            'Sell_Signal_Price': current_price if action_value < 0 else np.nan,
            'Net Worth': net_worth,
            'Balance': self.balance,
            'Position': self.position,
            'Reward': normalized_reward,
            'raw_reward': raw_reward,
            'Trade_Cost': trade_cost,
            'Profit_Reward': profit_reward,
            'Sharpe_Bonus': sharpe_bonus,
            'Forced_Stop_Penalty': forced_stop_penalty,
            'Forced_TP_Penalty': forced_tp_penalty,
            'Drawdown_Penalty': drawdown_penalty,
            'Transaction_Penalty': transaction_penalty,
            'Holding_Bonus': holding_bonus,
            'Favorable_Hold_Factor': favorable_hold_factor,
            'Invalid_Action_Penalty': invalid_act_penalty,
            'reward_scale': self.reward_weights.get('reward_scale', 1.0),
            'reward_norm_factor': self.reward_weights.get('reward_norm_factor', 1.0),
            'ema_alpha': self.ema_alpha
        })
        training_logger.debug(f"[Env {self.env_rank}] History appended at step {self.current_step}. Current History Length: {len(self.history)}")

        # --- Termination Check ---
        terminated = False
        truncated = False
        MIN_STEPS = 10
        if self.current_step >= MIN_STEPS:
            if net_worth <= 0:
                terminated = True
                normalized_reward -= 10.0
                training_logger.error(f"[Env {self.env_rank}] Bankruptcy occurred. Terminating episode at step {self.current_step}.")
            elif self.current_step >= len(self.df) - 1:
                terminated = True
                training_logger.info(f"[Env {self.env_rank}] Reached end of data at step {self.current_step}. Terminating episode.")

        # --- Update Step ---
        if not terminated:
            self.prev_net_worth = net_worth
            training_logger.debug(f"[Env {self.env_rank}] Before increment: Step {self.current_step}")
            self.current_step += 1
            training_logger.debug(f"[Env {self.env_rank}] After increment: Step {self.current_step}")
        else:
            training_logger.debug(f"[Env {self.env_rank}] Episode terminated at step {self.current_step}")
        self.current_step = min(self.current_step, len(self.df) - 1)
        obs = self._next_observation()

        # --- Periodic Logging ---
        if self.current_step % 100 == 0 or terminated:
            training_logger.debug(f"[Env {self.env_rank}] Step {self.current_step}: Reward = {normalized_reward:.4f}, Net Worth = {net_worth:.2f}, Balance = {self.balance:.2f}, Position = {self.position}")
            training_logger.info(f"[Env {self.env_rank}] Step {self.current_step}: Reward = {normalized_reward:.4f}, Net Worth = {net_worth:.2f}, Balance = {self.balance:.2f}, Position = {self.position}")

        training_logger.debug(f"[Env {self.env_rank}] After Action {action_value}: Balance = {self.balance}, Position = {self.position}, Net Worth = {net_worth}")

        return obs, normalized_reward, terminated, truncated, {}


##############################################
# Baseline Strategies
##############################################

def buy_and_hold_with_iloc(df: pd.DataFrame, initial_balance: float = 100000, transaction_cost: float = 0.001) -> Tuple[dict, pd.DataFrame]:
    """
    Implements a Buy and Hold strategy with DataFrame adjustments to prevent KeyError.

    Args:
        df (pd.DataFrame): DataFrame containing stock prices.
        initial_balance (float): Starting balance.
        transaction_cost (float): Transaction cost per trade.

    Returns:
        Tuple[dict, pd.DataFrame]: Results of the strategy and the transaction history DataFrame.
    """
    # Reset index without changing column cases
    df = df.reset_index(drop=True)

    # Save DataFrame before strategy execution
    bh_before_file = RESULTS_DIR / "buy_and_hold_before.csv"
    df.to_csv(bh_before_file, index=True)
    main_logger.info(f"[Strategy: Buy and Hold] DataFrame before strategy saved to {bh_before_file}")

    balance = initial_balance
    holdings = 0
    net_worth = initial_balance
    history = []

    required_cols = ['Close_unscaled']
    if not all(col in df.columns for col in required_cols):
        main_logger.error(f"[Strategy: Buy and Hold] Required columns are missing: {required_cols}")
        return {
            'Strategy': 'Buy and Hold',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    df = df.dropna(subset=required_cols)

    # Save DataFrame after dropping NaNs
    bh_after_dropna_file = RESULTS_DIR / "buy_and_hold_after_dropna.csv"
    df.to_csv(bh_after_dropna_file, index=True)
    main_logger.info(f"[Strategy: Buy and Hold] DataFrame after dropping NaNs saved to {bh_after_dropna_file}")

    # Ensure numeric types
    if not all(pd.api.types.is_numeric_dtype(df[col]) for col in required_cols):
        main_logger.error(f"[Strategy: Buy and Hold] Required columns have non-numeric data.")
        return {
            'Strategy': 'Buy and Hold',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    if 'Close_unscaled' not in df.columns:
        main_logger.error(f"[Strategy: Buy and Hold] 'Close_unscaled' column is missing.")
        return {
            'Strategy': 'Buy and Hold',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    try:
        # Invest the entire initial balance
        investment_percentage = 1.0  # 100% investment
        investment_amount = initial_balance * investment_percentage

        buy_price = df.iloc[0]['Close_unscaled']
        shares_to_buy = math.floor(investment_amount / buy_price)
        invested_capital = shares_to_buy * buy_price
        cost = shares_to_buy * buy_price * transaction_cost
        balance -= invested_capital + cost  # Remaining balance after buying
        holdings += shares_to_buy

        # Record the buy action
        history.append({
            'Date': df.iloc[0]['Date'],
            'Close_unscaled': buy_price,
            'Action': 'Buy',
            'Buy_Signal_Price': buy_price,
            'Sell_Signal_Price': np.nan,
            'Net Worth': balance + holdings * buy_price,
            'Balance': balance,
            'Position': holdings,
            'Reward': 0.0  # Initial buy, no reward yet
        })

        # Save DataFrame after buying
        bh_after_buy_file = RESULTS_DIR / "buy_and_hold_after_buy.csv"
        temp_df = df.copy()
        temp_df['Holdings'] = holdings
        temp_df['Balance'] = balance
        temp_df.to_csv(bh_after_buy_file, index=True)
        main_logger.info(f"[Strategy: Buy and Hold] DataFrame after buying saved to {bh_after_buy_file}")

        # Calculate final net worth
        final_price = df.iloc[-1]['Close_unscaled']
        net_worth = balance + holdings * final_price
        profit = net_worth - initial_balance

        # Record the sell action at the end
        history.append({
            'Date': df.iloc[-1]['Date'],
            'Close_unscaled': final_price,
            'Action': 'Sell',
            'Buy_Signal_Price': np.nan,
            'Sell_Signal_Price': final_price,
            'Net Worth': net_worth,
            'Balance': balance + holdings * final_price,
            'Position': 0,
            'Reward': profit / initial_balance  # Normalize profit as reward
        })

        main_logger.info(f"[Strategy: Buy and Hold] Bought {shares_to_buy} shares at {buy_price:.2f}")
        main_logger.info(f"[Strategy: Buy and Hold] Final Net Worth: ${net_worth:.2f}, Profit: ${profit:.2f}")
    except Exception as e:
        main_logger.error(f"[Strategy: Buy and Hold] Error during strategy execution: {e}")
        return {
            'Strategy': 'Buy and Hold',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    # Save history to DataFrame
    history_df = pd.DataFrame(history)

    # Save DataFrame after strategy execution
    bh_after_strategy_file = RESULTS_DIR / "buy_and_hold_after_strategy.csv"
    history_df.to_csv(bh_after_strategy_file, index=False)
    main_logger.info(f"[Strategy: Buy and Hold] History after strategy saved to {bh_after_strategy_file}")

    return {
        'Strategy': 'Buy and Hold',
        'Initial Balance': initial_balance,
        'Final Net Worth': net_worth,
        'Profit': profit
    }, history_df

def moving_average_crossover_with_iloc(df: pd.DataFrame, initial_balance: float = 100000, transaction_cost: float = 0.001, max_position_size: float = 0.5) -> Tuple[dict, pd.DataFrame]:
    """
    Implements a Moving Average Crossover strategy with DataFrame adjustments to prevent KeyError.

    Args:
        df (pd.DataFrame): DataFrame containing stock prices.
        initial_balance (float): Starting balance.
        transaction_cost (float): Transaction cost per trade.
        max_position_size (float): Maximum proportion of balance to use when buying.

    Returns:
        Tuple[dict, pd.DataFrame]: Results of the strategy and the transaction history DataFrame.
    """
    # Reset index without changing column cases
    df = df.reset_index(drop=True)

    # Save DataFrame before strategy execution
    ma_before_file = RESULTS_DIR / "moving_average_crossover_before.csv"
    df.to_csv(ma_before_file, index=True)
    main_logger.info(f"[Strategy: Moving Average Crossover] DataFrame before strategy saved to {ma_before_file}")

    balance = initial_balance
    holdings = 0
    net_worth = initial_balance
    history = []
    buy_price = 0.0
    sell_price = 0.0

    required_cols = ['SMA10_unscaled', 'SMA50_unscaled', 'Close_unscaled']
    if not all(col in df.columns for col in required_cols):
        main_logger.error(f"[Strategy: Moving Average Crossover] Required columns are missing: {required_cols}")
        return {
            'Strategy': 'Moving Average Crossover',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    df = df.dropna(subset=required_cols)

    # Save DataFrame after dropping NaNs
    ma_after_dropna_file = RESULTS_DIR / "moving_average_crossover_after_dropna.csv"
    df.to_csv(ma_after_dropna_file, index=True)
    main_logger.info(f"[Strategy: Moving Average Crossover] DataFrame after dropping NaNs saved to {ma_after_dropna_file}")

    for idx in range(1, len(df)):
        prev_sma10 = df.iloc[idx - 1]['SMA10_unscaled']
        prev_sma50 = df.iloc[idx - 1]['SMA50_unscaled']
        current_sma10 = df.iloc[idx]['SMA10_unscaled']
        current_sma50 = df.iloc[idx]['SMA50_unscaled']
        close_price = df.iloc[idx]['Close_unscaled']
        date = df.iloc[idx]['Date']

        # Buy signal: SMA10 crosses above SMA50
        if prev_sma10 < prev_sma50 and current_sma10 > current_sma50:
            # Buy signal
            investment_amount = balance * max_position_size
            shares_to_buy = math.floor(investment_amount / close_price)

            if shares_to_buy > 0:
                total_cost = shares_to_buy * close_price * (1 + transaction_cost)
                if total_cost <= balance:
                    balance -= total_cost
                    holdings += shares_to_buy
                    buy_price = close_price
                    history.append({
                        'Date': date,
                        'Close_unscaled': close_price,
                        'Action': 'Buy',
                        'Buy_Signal_Price': close_price,
                        'Sell_Signal_Price': np.nan,
                        'Net Worth': balance + holdings * close_price,
                        'Balance': balance,
                        'Position': holdings,
                        'Reward': 0.0
                    })
                    main_logger.debug(f"[Strategy: Moving Average Crossover] Bought {shares_to_buy} shares at {close_price:.2f} on {date}")
        # Sell signal: SMA10 crosses below SMA50
        elif prev_sma10 > prev_sma50 and current_sma10 < current_sma50:
            # Sell signal
            shares_to_sell = holdings  # Sell all holdings
            if shares_to_sell > 0:
                proceeds = shares_to_sell * close_price * (1 - transaction_cost)
                balance += proceeds
                holdings = 0
                sell_price = close_price
                net_worth = balance
                profit = (sell_price - buy_price) * shares_to_sell
                reward = profit / initial_balance
                history.append({
                    'Date': date,
                    'Close_unscaled': close_price,
                    'Action': 'Sell',
                    'Buy_Signal_Price': np.nan,
                    'Sell_Signal_Price': close_price,
                    'Net Worth': net_worth,
                    'Balance': balance,
                    'Position': holdings,
                    'Reward': reward
                })
                main_logger.debug(f"[Strategy: Moving Average Crossover] Sold {shares_to_sell} shares at {close_price:.2f} on {date}, Profit: ${profit:.2f}")

    # Calculate final net worth
    final_price = df.iloc[-1]['Close_unscaled']
    net_worth = balance + holdings * final_price
    profit = net_worth - initial_balance

    # Record final sell if holding any
    if holdings > 0:
        proceeds = holdings * final_price * (1 - transaction_cost)
        balance += proceeds
        profit += (final_price - buy_price) * holdings
        history.append({
            'Date': df.iloc[-1]['Date'],
            'Close_unscaled': final_price,
            'Action': 'Sell',
            'Buy_Signal_Price': np.nan,
            'Sell_Signal_Price': final_price,
            'Net Worth': balance,
            'Balance': balance,
            'Position': 0,
            'Reward': ((final_price - buy_price) * holdings) / initial_balance
        })
        main_logger.debug(f"[Strategy: Moving Average Crossover] Final sell of {holdings} shares at {final_price:.2f} on {df.iloc[-1]['Date']}")

    main_logger.info(f"[Strategy: Moving Average Crossover] Final Net Worth: ${net_worth:.2f}, Profit: ${profit:.2f}")

    # Save history to DataFrame
    history_df = pd.DataFrame(history)

    # Save DataFrame after strategy execution
    ma_after_strategy_file = RESULTS_DIR / "moving_average_crossover_after_strategy.csv"
    history_df.to_csv(ma_after_strategy_file, index=False)
    main_logger.info(f"[Strategy: Moving Average Crossover] History after strategy saved to {ma_after_strategy_file}")

    return {
        'Strategy': 'Moving Average Crossover',
        'Initial Balance': initial_balance,
        'Final Net Worth': net_worth,
        'Profit': profit
    }, history_df

def macd_strategy_with_iloc(df: pd.DataFrame, initial_balance: float = 100000, transaction_cost: float = 0.001, max_position_size: float = 0.5) -> Tuple[dict, pd.DataFrame]:
    """
    Implements a MACD Crossover strategy with DataFrame adjustments to prevent KeyError.

    Args:
        df (pd.DataFrame): DataFrame containing stock prices.
        initial_balance (float): Starting balance.
        transaction_cost (float): Transaction cost per trade.
        max_position_size (float): Maximum proportion of balance to use when buying.

    Returns:
        Tuple[dict, pd.DataFrame]: Results of the strategy and the transaction history DataFrame.
    """
    # Reset index without changing column cases
    df = df.reset_index(drop=True)

    # Save DataFrame before strategy execution
    macd_before_file = RESULTS_DIR / "macd_strategy_before.csv"
    df.to_csv(macd_before_file, index=True)
    main_logger.info(f"[Strategy: MACD Crossover] DataFrame before strategy saved to {macd_before_file}")

    balance = initial_balance
    holdings = 0
    net_worth = initial_balance
    history = []
    buy_price = 0.0
    sell_price = 0.0

    required_cols = ['MACD_unscaled', 'Close_unscaled']
    if not all(col in df.columns for col in required_cols):
        main_logger.error(f"[Strategy: MACD Crossover] Required columns are missing: {required_cols}")
        return {
            'Strategy': 'MACD Crossover',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    df = df.dropna(subset=required_cols)

    # Save DataFrame after dropping NaNs
    macd_after_dropna_file = RESULTS_DIR / "macd_strategy_after_dropna.csv"
    df.to_csv(macd_after_dropna_file, index=True)
    main_logger.info(f"[Strategy: MACD Crossover] DataFrame after dropping NaNs saved to {macd_after_dropna_file}")

    for idx in range(1, len(df)):
        prev_macd = df.iloc[idx - 1]['MACD_unscaled']
        current_macd = df.iloc[idx]['MACD_unscaled']
        close_price = df.iloc[idx]['Close_unscaled']
        date = df.iloc[idx]['Date']

        # Buy signal: MACD crosses above zero line
        if prev_macd < 0 and current_macd > 0:
            # Buy signal
            investment_amount = balance * max_position_size
            shares_to_buy = math.floor(investment_amount / close_price)

            if shares_to_buy > 0:
                total_cost = shares_to_buy * close_price * (1 + transaction_cost)
                if total_cost <= balance:
                    balance -= total_cost
                    holdings += shares_to_buy
                    buy_price = close_price
                    history.append({
                        'Date': date,
                        'Close_unscaled': close_price,
                        'Action': 'Buy',
                        'Buy_Signal_Price': close_price,
                        'Sell_Signal_Price': np.nan,
                        'Net Worth': balance + holdings * close_price,
                        'Balance': balance,
                        'Position': holdings,
                        'Reward': 0.0
                    })
                    main_logger.debug(f"[Strategy: MACD Crossover] Bought {shares_to_buy} shares at {close_price:.2f} on {date}")
        # Sell signal: MACD crosses below zero line
        elif prev_macd > 0 and current_macd < 0:
            # Sell signal
            shares_to_sell = holdings  # Sell all holdings
            if shares_to_sell > 0:
                proceeds = shares_to_sell * close_price * (1 - transaction_cost)
                balance += proceeds
                holdings = 0
                sell_price = close_price
                net_worth = balance
                profit = (sell_price - buy_price) * shares_to_sell
                reward = profit / initial_balance
                history.append({
                    'Date': date,
                    'Close_unscaled': close_price,
                    'Action': 'Sell',
                    'Buy_Signal_Price': np.nan,
                    'Sell_Signal_Price': close_price,
                    'Net Worth': net_worth,
                    'Balance': balance,
                    'Position': holdings,
                    'Reward': reward
                })
                main_logger.debug(f"[Strategy: MACD Crossover] Sold {shares_to_sell} shares at {close_price:.2f} on {date}, Profit: ${profit:.2f}")

    # Calculate final net worth
    final_price = df.iloc[-1]['Close_unscaled']
    net_worth = balance + holdings * final_price
    profit = net_worth - initial_balance

    # Record final sell if holding any
    if holdings > 0:
        proceeds = holdings * final_price * (1 - transaction_cost)
        balance += proceeds
        profit += (final_price - buy_price) * holdings
        history.append({
            'Date': df.iloc[-1]['Date'],
            'Close_unscaled': final_price,
            'Action': 'Sell',
            'Buy_Signal_Price': np.nan,
            'Sell_Signal_Price': final_price,
            'Net Worth': balance,
            'Balance': balance,
            'Position': 0,
            'Reward': ((final_price - buy_price) * holdings) / initial_balance
        })
        main_logger.debug(f"[Strategy: MACD Crossover] Final sell of {holdings} shares at {final_price:.2f} on {df.iloc[-1]['Date']}")

    main_logger.info(f"[Strategy: MACD Crossover] Final Net Worth: ${net_worth:.2f}, Profit: ${profit:.2f}")

    # Save history to DataFrame
    history_df = pd.DataFrame(history)

    # Save DataFrame after strategy execution
    macd_after_strategy_file = RESULTS_DIR / "macd_strategy_after_strategy.csv"
    history_df.to_csv(macd_after_strategy_file, index=False)
    main_logger.info(f"[Strategy: MACD Crossover] History after strategy saved to {macd_after_strategy_file}")

    return {
        'Strategy': 'MACD Crossover',
        'Initial Balance': initial_balance,
        'Final Net Worth': net_worth,
        'Profit': profit
    }, history_df

def bollinger_bands_strategy_with_iloc(df: pd.DataFrame, initial_balance: float = 100000, 
                                       transaction_cost: float = 0.001, max_position_size: float = 0.5) -> Tuple[dict, pd.DataFrame]:
    """
    Implements a Bollinger Bands strategy using DataFrame indexing.

    Args:
        df (pd.DataFrame): DataFrame containing stock prices.
        initial_balance (float): Starting balance.
        transaction_cost (float): Transaction cost per trade.
        max_position_size (float): Maximum proportion of balance to use when buying.

    Returns:
        Tuple[dict, pd.DataFrame]: Results of the strategy and the transaction history DataFrame.
    """
    import math  # Ensure math is imported
    # Reset index to ensure proper row alignment
    df = df.reset_index(drop=True)

    balance = initial_balance
    holdings = 0
    net_worth = initial_balance
    history = []
    buy_price = None  # Initialize as None to handle cases with no prior buy

    required_cols = ['BB_Upper_unscaled', 'BB_Lower_unscaled', 'Close_unscaled']
    if not all(col in df.columns for col in required_cols):
        main_logger.error(f"[Strategy: Bollinger Bands] Required columns are missing: {required_cols}")
        return {
            'Strategy': 'Bollinger Bands',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    df = df.dropna(subset=required_cols)

    # Save DataFrame after dropping NaNs
    bb_after_dropna_file = RESULTS_DIR / "bollinger_bands_after_dropna.csv"
    df.to_csv(bb_after_dropna_file, index=True)
    main_logger.info(f"[Strategy: Bollinger Bands] DataFrame after dropping NaNs saved to {bb_after_dropna_file}")

    for idx in range(1, len(df)):
        prev_close = df.iloc[idx - 1]['Close_unscaled']
        prev_bb_lower = df.iloc[idx - 1]['BB_Lower_unscaled']
        prev_bb_upper = df.iloc[idx - 1]['BB_Upper_unscaled']
        current_close = df.iloc[idx]['Close_unscaled']
        current_bb_upper = df.iloc[idx]['BB_Upper_unscaled']
        current_bb_lower = df.iloc[idx]['BB_Lower_unscaled']
        date = df.iloc[idx]['Date']

        # Buy signal: Price crosses below lower Bollinger Band
        if prev_close >= prev_bb_lower and current_close < current_bb_lower:
            # Buy shares with max position size
            investment_amount = balance * max_position_size
            shares_to_buy = math.floor(investment_amount / current_close)

            if shares_to_buy > 0:
                total_cost = shares_to_buy * current_close * (1 + transaction_cost)
                if total_cost <= balance:
                    balance -= total_cost
                    holdings += shares_to_buy
                    buy_price = current_close  # Update buy_price only on successful buy
                    history.append({
                        'Date': date,
                        'Close_unscaled': current_close,
                        'Action': 'Buy',
                        'Buy_Signal_Price': current_close,
                        'Sell_Signal_Price': None,
                        'Net Worth': balance + holdings * current_close,
                        'Balance': balance,
                        'Position': holdings,
                        'Reward': 0.0  # Reward placeholder
                    })
                    main_logger.debug(f"[Strategy: Bollinger Bands] Bought {shares_to_buy} shares at {current_close:.2f} on {date}")

        # Sell signal: Price crosses above upper Bollinger Band
        elif prev_close <= prev_bb_upper and current_close > current_bb_upper:
            # Sell signal
            if holdings > 0 and buy_price is not None:
                shares_to_sell = holdings  # Sell all holdings
                proceeds = shares_to_sell * current_close * (1 - transaction_cost)
                profit = (current_close - buy_price) * shares_to_sell
                balance += proceeds
                net_worth = balance
                reward = profit / initial_balance
                holdings = 0
                sell_price = current_close
                history.append({
                    'Date': date,
                    'Close_unscaled': current_close,
                    'Action': 'Sell',
                    'Buy_Signal_Price': None,
                    'Sell_Signal_Price': current_close,
                    'Net Worth': net_worth,
                    'Balance': balance,
                    'Position': holdings,
                    'Reward': reward
                })
                main_logger.debug(f"[Strategy: Bollinger Bands] Sold {shares_to_sell} shares at {current_close:.2f} on {date}, Profit: ${profit:.2f}")
            else:
                main_logger.debug(f"[Strategy: Bollinger Bands] Sell signal triggered but no holdings to sell or buy_price is undefined.")

    # Calculate final net worth
    final_price = df.iloc[-1]['Close_unscaled']
    net_worth = balance + holdings * final_price
    profit = net_worth - initial_balance

    # Record final sell if holding any
    if holdings > 0 and buy_price is not None:
        shares_to_sell = holdings
        proceeds = shares_to_sell * final_price * (1 - transaction_cost)
        profit += (final_price - buy_price) * shares_to_sell
        balance += proceeds
        net_worth = balance
        reward = (final_price - buy_price) * shares_to_sell / initial_balance
        history.append({
            'Date': df.iloc[-1]['Date'],
            'Close_unscaled': final_price,
            'Action': 'Sell',
            'Buy_Signal_Price': None,
            'Sell_Signal_Price': final_price,
            'Net Worth': net_worth,
            'Balance': balance,
            'Position': 0,
            'Reward': reward
        })
        main_logger.debug(f"[Strategy: Bollinger Bands] Final sell of {shares_to_sell} shares at {final_price:.2f} on {df.iloc[-1]['Date']}")

    # Convert history to DataFrame
    history_df = pd.DataFrame(history)

    # Save history to DataFrame
    bb_after_strategy_file = RESULTS_DIR / "bollinger_bands_after_strategy.csv"
    history_df.to_csv(bb_after_strategy_file, index=False)
    main_logger.info(f"[Strategy: Bollinger Bands] History after strategy saved to {bb_after_strategy_file}")

    return {
        'Strategy': 'Bollinger Bands',
        'Initial Balance': initial_balance,
        'Final Net Worth': net_worth,
        'Profit': profit
    }, history_df

def random_strategy_with_iloc(df: pd.DataFrame, initial_balance: float = 100000, transaction_cost: float = 0.001, max_position_size: float = 0.5) -> Tuple[dict, pd.DataFrame]:
    """
    Implements a Random trading strategy with DataFrame adjustments to prevent KeyError.

    Args:
        df (pd.DataFrame): DataFrame containing stock prices.
        initial_balance (float): Starting balance.
        transaction_cost (float): Transaction cost per trade.
        max_position_size (float): Maximum proportion of balance to use when buying.

    Returns:
        Tuple[dict, pd.DataFrame]: Results of the strategy and the transaction history DataFrame.
    """
    # Reset index without changing column cases
    df = df.reset_index(drop=True)

    # Save DataFrame before strategy execution
    random_before_file = RESULTS_DIR / "random_strategy_before.csv"
    df.to_csv(random_before_file, index=True)
    main_logger.info(f"[Strategy: Random] DataFrame before strategy saved to {random_before_file}")

    balance = initial_balance
    holdings = 0
    net_worth = initial_balance
    history = []
    buy_price = 0.0
    sell_price = 0.0

    required_cols = ['Close_unscaled']
    if not all(col in df.columns for col in required_cols):
        main_logger.error(f"[Strategy: Random] Required columns are missing: {required_cols}")
        return {
            'Strategy': 'Random Strategy',
            'Initial Balance': initial_balance,
            'Final Net Worth': net_worth,
            'Profit': 0.0
        }, pd.DataFrame()

    df = df.dropna(subset=required_cols)

    # Save DataFrame after dropping NaNs
    random_after_dropna_file = RESULTS_DIR / "random_strategy_after_dropna.csv"
    df.to_csv(random_after_dropna_file, index=True)
    main_logger.info(f"[Strategy: Random] DataFrame after dropping NaNs saved to {random_after_dropna_file}")

    for idx in range(1, len(df)):
        action = random.choice(['Buy', 'Sell', 'Hold'])
        close_price = df.iloc[idx]['Close_unscaled']
        date = df.iloc[idx]['Date']

        if action == 'Buy':
            investment_amount = balance * max_position_size
            shares_to_buy = math.floor(investment_amount / close_price)

            if shares_to_buy > 0:
                total_cost = shares_to_buy * close_price * (1 + transaction_cost)
                if total_cost <= balance:
                    balance -= total_cost
                    holdings += shares_to_buy
                    buy_price = close_price
                    history.append({
                        'Date': date,
                        'Close_unscaled': close_price,
                        'Action': 'Buy',
                        'Buy_Signal_Price': close_price,
                        'Sell_Signal_Price': np.nan,
                        'Net Worth': balance + holdings * close_price,
                        'Balance': balance,
                        'Position': holdings,
                        'Reward': 0.0
                    })
                    main_logger.debug(f"[Strategy: Random] Bought {shares_to_buy} shares at {close_price:.2f} on {date}")
        elif action == 'Sell':
            shares_to_sell = holdings  # Sell all holdings
            if shares_to_sell > 0:
                proceeds = shares_to_sell * close_price * (1 - transaction_cost)
                balance += proceeds
                holdings = 0
                sell_price = close_price
                net_worth = balance
                profit = (sell_price - buy_price) * shares_to_sell
                reward = profit / initial_balance
                history.append({
                    'Date': date,
                    'Close_unscaled': close_price,
                    'Action': 'Sell',
                    'Buy_Signal_Price': np.nan,
                    'Sell_Signal_Price': close_price,
                    'Net Worth': net_worth,
                    'Balance': balance,
                    'Position': holdings,
                    'Reward': reward
                })
                main_logger.debug(f"[Strategy: Random] Sold {shares_to_sell} shares at {close_price:.2f} on {date}, Profit: ${profit:.2f}")
        else:
            # Hold action; no operation
            history.append({
                'Date': date,
                'Close_unscaled': close_price,
                'Action': 'Hold',
                'Buy_Signal_Price': np.nan,
                'Sell_Signal_Price': np.nan,
                'Net Worth': balance + holdings * close_price,
                'Balance': balance,
                'Position': holdings,
                'Reward': 0.0
            })
            main_logger.debug(f"[Strategy: Random] Held position on {date}")

    # Calculate final net worth
    final_price = df.iloc[-1]['Close_unscaled']
    net_worth = balance + holdings * final_price
    profit = net_worth - initial_balance

    # Record final sell if holding any
    if holdings > 0:
        proceeds = holdings * final_price * (1 - transaction_cost)
        balance += proceeds
        profit += (final_price - buy_price) * holdings
        history.append({
            'Date': df.iloc[-1]['Date'],
            'Close_unscaled': final_price,
            'Action': 'Sell',
            'Buy_Signal_Price': np.nan,
            'Sell_Signal_Price': final_price,
            'Net Worth': balance,
            'Balance': balance,
            'Position': 0,
            'Reward': ((final_price - buy_price) * holdings) / initial_balance
        })
        main_logger.debug(f"[Strategy: Random] Final sell of {holdings} shares at {final_price:.2f} on {df.iloc[-1]['Date']}")

    main_logger.info(f"[Strategy: Random] Final Net Worth: ${net_worth:.2f}, Profit: ${profit:.2f}")

    # Save history to DataFrame
    history_df = pd.DataFrame(history)

    # Save DataFrame after strategy execution
    random_after_strategy_file = RESULTS_DIR / "random_strategy_after_strategy.csv"
    history_df.to_csv(random_after_strategy_file, index=False)
    main_logger.info(f"[Strategy: Random] History after strategy saved to {random_after_strategy_file}")

    return {
        'Strategy': 'Random Strategy',
        'Initial Balance': initial_balance,
        'Final Net Worth': net_worth,
        'Profit': profit
    }, history_df

##############################################
# Plotting Functions
##############################################

def plot_rl_training_history(training_history: pd.DataFrame, pdf: PdfPages):
    """
    Plots the RL Agent's training history, including net worth and rewards over time.

    Args:
        training_history (pd.DataFrame): DataFrame containing the RL agent's training history.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    if training_history.empty:
        main_logger.error("RL training history is empty. Cannot plot training history.")
        return

    plt.figure(figsize=(14, 7))
    sns.set_style("darkgrid")

    sns.lineplot(x='Date', y='Net Worth', data=training_history, label='Net Worth')
    sns.lineplot(x='Date', y='Reward', data=training_history, label='Reward', color='orange')

    plt.title('RL Agent Training History')
    plt.xlabel('Date')
    plt.ylabel('Value')
    plt.legend()
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("RL training history plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving RL training history plot: {e}")
    finally:
        plt.close()

def plot_reward_movements(test_history: pd.DataFrame, pdf: PdfPages):
    """
    Plots the reward movements of the RL Agent over the test period.

    Args:
        test_history (pd.DataFrame): DataFrame containing the RL agent's test trading history.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    if test_history.empty:
        main_logger.error("RL test history is empty. Cannot plot reward movements.")
        return

    plt.figure(figsize=(14, 7))
    sns.set_style("darkgrid")

    sns.lineplot(x='Date', y='Reward', data=test_history, label='Reward', color='green')

    plt.title('RL Agent Reward Movements Over Test Period')
    plt.xlabel('Date')
    plt.ylabel('Reward')
    plt.legend()
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Reward movements plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving reward movements plot: {e}")
    finally:
        plt.close()

def plot_position_movements(test_history: pd.DataFrame, pdf: PdfPages):
    """
    Plots the position movements of the RL Agent over the test period.

    Args:
        test_history (pd.DataFrame): DataFrame containing the RL agent's test trading history.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    if test_history.empty:
        main_logger.error("RL test history is empty. Cannot plot position movements.")
        return

    plt.figure(figsize=(14, 7))
    sns.set_style("darkgrid")

    sns.lineplot(x='Date', y='Position', data=test_history, label='Position (Shares)', color='purple')

    plt.title('RL Agent Position Movements Over Time')
    plt.xlabel('Date')
    plt.ylabel('Position (Shares)')
    plt.legend()
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Position movements plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving position movements plot: {e}")
    finally:
        plt.close()

def plot_drawdown_movements(test_history: pd.DataFrame, pdf: PdfPages):
    """
    Plots the drawdown movements of the RL Agent over the test period.

    Args:
        test_history (pd.DataFrame): DataFrame containing the RL agent's test trading history.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    if test_history.empty:
        main_logger.error("RL test history is empty. Cannot plot drawdown movements.")
        return

    net_worth_series = test_history['Net Worth']
    rolling_max = net_worth_series.cummax()
    drawdown = (net_worth_series - rolling_max) / rolling_max

    plt.figure(figsize=(14, 7))
    sns.set_style("darkgrid")

    sns.lineplot(x=test_history['Date'], y=drawdown, label='Drawdown', color='red')

    plt.title('RL Agent Drawdown Movements Over Time')
    plt.xlabel('Date')
    plt.ylabel('Drawdown')
    plt.legend()
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Drawdown movements plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving drawdown movements plot: {e}")
    finally:
        plt.close()

def plot_all_buy_sell_signals(strategy_history: dict, pdf: PdfPages):
    """
    Plots buy and sell signals for each strategy along with relevant technical indicators.

    Args:
        strategy_history (dict): Dictionary containing strategy names as keys and their history DataFrames as values.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    for strategy, history_df in strategy_history.items():
        if history_df.empty:
            main_logger.warning(f"History for strategy '{strategy}' is empty. Skipping plot.")
            continue

        plt.figure(figsize=(14, 7))
        sns.set_style("darkgrid")

        dates = history_df['Date']
        close_prices = history_df['Close_unscaled']
        plt.plot(dates, close_prices, label='Close Price', color='blue')

        # Plot Buy and Sell Signals
        buy_signals = history_df[history_df['Action'] == 'Buy']
        sell_signals = history_df[history_df['Action'] == 'Sell']

        plt.scatter(buy_signals['Date'], buy_signals['Close_unscaled'], marker='^', color='green', label='Buy Signal', alpha=1)
        plt.scatter(sell_signals['Date'], sell_signals['Close_unscaled'], marker='v', color='red', label='Sell Signal', alpha=1)

        # Add technical indicators based on strategy
        if strategy == 'Moving Average Crossover':
            if 'SMA10_unscaled' in history_df.columns and 'SMA50_unscaled' in history_df.columns:
                plt.plot(history_df['Date'], history_df['SMA10_unscaled'], label='SMA10', color='orange')
                plt.plot(history_df['Date'], history_df['SMA50_unscaled'], label='SMA50', color='magenta')
        elif strategy == 'MACD Crossover':
            if 'MACD_unscaled' in history_df.columns:
                plt.plot(history_df['Date'], history_df['MACD_unscaled'], label='MACD', color='purple')
        elif strategy == 'Bollinger Bands':
            if 'BB_Upper_unscaled' in history_df.columns and 'BB_Lower_unscaled' in history_df.columns:
                plt.plot(history_df['Date'], history_df['BB_Upper_unscaled'], label='Bollinger Upper Band', color='cyan')
                plt.plot(history_df['Date'], history_df['BB_Lower_unscaled'], label='Bollinger Lower Band', color='cyan')

        plt.title(f'{strategy} - Buy and Sell Signals on Test Data')
        plt.xlabel('Date')
        plt.ylabel('Price')
        plt.legend()
        plt.tight_layout()

        try:
            pdf.savefig()
            main_logger.info(f"Buy and Sell signals plotted successfully for strategy '{strategy}'.")
        except Exception as e:
            main_logger.error(f"Error saving Buy and Sell signals plot for strategy '{strategy}': {e}")
            continue
        finally:
            plt.close()

def plot_profit_comparison(strategy_results: list, pdf: PdfPages):
    """
    Plots a comparison of profits across all strategies.

    Args:
        strategy_results (list): List of tuples containing strategy results and their history DataFrames.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    plt.figure(figsize=(10, 6))
    sns.set_style("whitegrid")

    strategies = [result[0]['Strategy'] for result in strategy_results]
    profits = [result[0]['Profit'] for result in strategy_results]

    sns.barplot(x=strategies, y=profits, palette='viridis')

    plt.title('Profit Comparison Among Strategies')
    plt.xlabel('Strategy')
    plt.ylabel('Profit ($)')
    plt.xticks(rotation=45)
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Profit comparison plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving profit comparison plot: {e}")
    finally:
        plt.close()

def plot_transaction_count(strategy_results: list, pdf: PdfPages):
    """
    Plots the number of transactions made by each strategy.

    Args:
        strategy_results (list): List of tuples containing strategy results and their history DataFrames.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    transaction_counts = []
    strategies = []

    for result, history_df in strategy_results:
        if history_df.empty:
            count = 0
        else:
            count = history_df['Action'].value_counts().get('Buy', 0) + history_df['Action'].value_counts().get('Sell', 0)
        transaction_counts.append(count)
        strategies.append(result['Strategy'])

    plt.figure(figsize=(10, 6))
    sns.set_style("whitegrid")

    sns.barplot(x=strategies, y=transaction_counts, palette='magma')

    plt.title('Transaction Count per Strategy')
    plt.xlabel('Strategy')
    plt.ylabel('Number of Transactions')
    plt.xticks(rotation=45)
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Transaction count plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving transaction count plot: {e}")
    finally:
        plt.close()

def plot_cash_balance(strategy_results: list, pdf: PdfPages):
    """
    Plots the final net worth of each strategy.

    Args:
        strategy_results (list): List of tuples containing strategy results and their history DataFrames.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    plt.figure(figsize=(10, 6))
    sns.set_style("whitegrid")

    strategies = [result[0]['Strategy'] for result in strategy_results]
    net_worths = [result[0]['Final Net Worth'] for result in strategy_results]

    sns.barplot(x=strategies, y=net_worths, palette='coolwarm')

    plt.title('Final Net Worth per Strategy')
    plt.xlabel('Strategy')
    plt.ylabel('Net Worth ($)')
    plt.xticks(rotation=45)
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Final net worth plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving final net worth plot: {e}")
    finally:
        plt.close()

def plot_transaction_costs(strategy_results: list, pdf: PdfPages):
    """
    Plots the total transaction costs incurred by each strategy.

    Args:
        strategy_results (list): List of tuples containing strategy results and their history DataFrames.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    transaction_costs = []
    strategies = []

    for result, history_df in strategy_results:
        if history_df.empty:
            cost = 0.0
        else:
            buys = history_df[history_df['Action'] == 'Buy']
            sells = history_df[history_df['Action'] == 'Sell']
            cost = buys.shape[0] * 0.001 + sells.shape[0] * 0.001  # Assuming 0.1% cost per transaction
        transaction_costs.append(cost)
        strategies.append(result['Strategy'])

    plt.figure(figsize=(10, 6))
    sns.set_style("whitegrid")

    sns.barplot(x=strategies, y=transaction_costs, palette='inferno')

    plt.title('Transaction Costs per Strategy')
    plt.xlabel('Strategy')
    plt.ylabel('Transaction Costs ($)')
    plt.xticks(rotation=45)
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("Transaction costs plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving transaction costs plot: {e}")
    finally:
        plt.close()

def plot_comparison(test_df: pd.DataFrame, rl_test_df: pd.DataFrame, strategy_results: list, initial_balance: float, ticker: str, pdf: PdfPages):
    """
    Plots RL Agent vs Baseline Strategies on the test data.

    Args:
        test_df (pd.DataFrame): Test data DataFrame.
        rl_test_df (pd.DataFrame): RL agent's test history DataFrame.
        strategy_results (list): List of tuples containing strategy results and their history DataFrames.
        initial_balance (float): Initial balance for all strategies.
        ticker (str): Stock ticker symbol.
        pdf (PdfPages): PdfPages object to save the plot.
    """
    plt.figure(figsize=(14, 7))
    sns.set_style("darkgrid")

    # Plot RL Agent's Net Worth
    if not rl_test_df.empty:
        sns.lineplot(x='Date', y='Net Worth', data=rl_test_df, label='RL Agent', color='blue')

    # Plot Baseline Strategies' Net Worth
    for result, history_df in strategy_results:
        if history_df.empty:
            continue
        sns.lineplot(x='Date', y='Net Worth', data=history_df, label=result['Strategy'])

    plt.title('RL Agent vs Baseline Strategies - Net Worth Comparison')
    plt.xlabel('Date')
    plt.ylabel('Net Worth ($)')
    plt.legend()
    plt.tight_layout()

    try:
        pdf.savefig()
        main_logger.info("RL Agent vs Baseline Strategies Net Worth comparison plotted successfully.")
    except Exception as e:
        main_logger.error(f"Error saving RL vs Baseline Net Worth comparison plot: {e}")
    finally:
        plt.close()

##############################################
# Callbacks
##############################################

class EarlyStoppingCallback(BaseCallback):
    """
    Custom callback for implementing early stopping based on the normalized reward.
    Stops training if the normalized reward does not improve for a given number of evaluations (patience).
    """
    def __init__(self, monitor='train/reward_env', patience=20, min_delta=1e-5, verbose=1):
        super(EarlyStoppingCallback, self).__init__(verbose)
        self.monitor = monitor
        self.patience = patience
        self.min_delta = min_delta
        self.best_reward = -np.inf
        self.wait = 0

    def _on_step(self) -> bool:
        # Retrieve the latest value of the monitored metric
        current_reward = self.logger.name_to_value.get(self.monitor, None)

        if current_reward is None:
            if self.verbose > 0:
                print(f"EarlyStoppingCallback: Metric '{self.monitor}' not found.")
            return True  # Continue training

        if current_reward > self.best_reward + self.min_delta:
            self.best_reward = current_reward
            self.wait = 0
            if self.verbose > 0:
                print(f"EarlyStoppingCallback: Reward improved to {self.best_reward:.4f}. Resetting wait counter.")
        else:
            self.wait += 1
            if self.verbose > 0:
                print(f"EarlyStoppingCallback: No improvement in reward. Wait counter: {self.wait}/{self.patience}")
            if self.wait >= self.patience:
                if self.verbose > 0:
                    print("EarlyStoppingCallback: Patience exceeded. Stopping training.")
                return False  # Stop training

        return True  # Continue training

class CustomTensorboardCallback(BaseCallback):
    """
    Custom callback for logging a rolling average of rewards to TensorBoard,
    as well as final metrics at the end of training.
    """
    def __init__(self, verbose=0, window_size=100):
        super(CustomTensorboardCallback, self).__init__(verbose)
        self.window_size = window_size
        self.rewards_buffer = []
        self.start_time = None

    def _on_training_start(self) -> None:
        self.start_time = time.time()

    def _on_step(self) -> bool:
        # Access the environment
        env = self.training_env.envs[0]

        # Log rolling average of reward if history is available
        if hasattr(env, 'history') and env.history:
            last_step = env.history[-1]
            recent_reward = last_step.get('Reward', 0.0)
            self.rewards_buffer.append(recent_reward)

            # Keep the buffer size limited to 'window_size'
            if len(self.rewards_buffer) > self.window_size:
                self.rewards_buffer.pop(0)

            # Calculate the rolling average reward
            rolling_avg_reward = np.mean(self.rewards_buffer)

            # Record it under "train/reward_env" so early stopping can monitor this
            self.logger.record("train/reward_env", rolling_avg_reward)

            # Log additional metrics like net worth, balance, or position if desired
            self.logger.record("train/net_worth_env", last_step.get('Net Worth', 0.0))
            self.logger.record("train/balance_env", last_step.get('Balance', 0.0))
            self.logger.record("train/position_env", last_step.get('Position', 0.0))

        # Log elapsed time
        if self.start_time:
            elapsed_time = time.time() - self.start_time
            formatted_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
            self.logger.record("train/elapsed_time_env", elapsed_time)
            self.logger.record("train/elapsed_time_formatted_env", formatted_time)

        return True

    def _on_training_end(self) -> None:
        # Access the environment
        env = self.training_env.envs[0]

        # Log final metrics at the end of training
        if hasattr(env, 'history') and env.history:
            # Final net worth, balance, etc.
            self.logger.record("train/final_net_worth", env.history[-1].get('Net Worth', 0.0))
            self.logger.record("train/final_reward", sum(h['Reward'] for h in env.history))
            self.logger.record("train/final_balance", env.history[-1].get('Balance', 0.0))
            self.logger.record("train/final_position", env.history[-1].get('Position', 0.0))

            # Optionally compute a final rolling average over last 'window_size' steps
            rewards_slice = [step.get('Reward', 0.0) for step in env.history[-self.window_size:]]
            if rewards_slice:
                final_rolling_avg = np.mean(rewards_slice)
            else:
                final_rolling_avg = 0.0
            self.logger.record("train/final_rolling_avg_reward", final_rolling_avg)

        else:
            # If no history or environment is empty, log zeros
            self.logger.record("train/final_net_worth", 0.0)
            self.logger.record("train/final_reward", 0.0)
            self.logger.record("train/final_balance", 0.0)
            self.logger.record("train/final_position", 0.0)
            self.logger.record("train/final_rolling_avg_reward", 0.0)


##############################################
# Additional Utility Functions
##############################################

def calculate_max_drawdown(net_worth_series: pd.Series) -> float:
    """
    Calculates the Maximum Drawdown of a net worth series.

    Args:
        net_worth_series (pd.Series): Series of net worth over time.

    Returns:
        float: Maximum drawdown value.
    """
    rolling_max = net_worth_series.cummax()
    drawdown = (net_worth_series - rolling_max) / rolling_max
    return drawdown.min()

def calculate_annualized_return(net_worth_series: pd.Series, periods_per_year: int = 252) -> float:
    """
    Calculates the Annualized Return (CAGR).

    Args:
        net_worth_series (pd.Series): Series of net worth over time.
        periods_per_year (int): Number of trading periods in a year.

    Returns:
        float: Annualized return.
    """
    start_value = net_worth_series.iloc[0]
    end_value = net_worth_series.iloc[-1]
    num_periods = len(net_worth_series)
    if num_periods == 0:
        return 0.0
    return (end_value / start_value) ** (periods_per_year / num_periods) - 1

##############################################
# Optuna Hyperparameter Tuning
##############################################

def generate_unique_study_name(base_name='rl_trading_agent_study'):
    """
    Generates a unique study name by appending the current timestamp.

    Args:
        base_name (str): The base name for the study.

    Returns:
        str: A unique study name.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base_name}_{timestamp}"

def objective(trial, df, scaler, initial_balance, stop_loss, take_profit, max_position_size,
              max_drawdown, annual_trading_days, transaction_cost):
    learning_rate = trial.suggest_loguniform('learning_rate', 1e-6, 1e-3)
    n_steps = trial.suggest_categorical('n_steps', [128, 256, 512])
    batch_size = trial.suggest_categorical('batch_size', [32, 64])
    gamma = trial.suggest_uniform('gamma', 0.98, 0.999)
    gae_lambda = trial.suggest_uniform('gae_lambda', 0.80, 1.00)
    clip_range = trial.suggest_uniform('clip_range', 0.1, 0.3)
    ent_coef = trial.suggest_loguniform('ent_coef', 1e-4, 1e-1)
    vf_coef = trial.suggest_uniform('vf_coef', 0.1, 0.5)
    max_grad_norm = trial.suggest_uniform('max_grad_norm', 0.5, 1.0)
    net_arch = trial.suggest_categorical('net_arch', ['128_128', '256_256', '128_256_128'])

    drawdown_penalty_factor = trial.suggest_float('drawdown_penalty_factor', 0.0001, 1.0, log=True)

    tuned_stop_loss = trial.suggest_float('stop_loss', 0.80, 0.95, step=0.01)
    tuned_take_profit = trial.suggest_float('take_profit', 1.05, 1.20, step=0.01)
    tuned_transaction_cost = trial.suggest_float('transaction_cost', 0.0005, 0.005, step=0.0005)
    tuned_reward_scale = trial.suggest_float('reward_scale', 0.5, 2.0, step=0.1)
    tuned_max_position_size = trial.suggest_float('max_position_size', 0.1, 1.0, step=0.1)
    tuned_max_drawdown = trial.suggest_float('max_drawdown', 0.1, 0.3, step=0.01)

    profit_weight = trial.suggest_float('profit_weight', 0.5, 1000.0)
    sharpe_bonus_weight = trial.suggest_float('sharpe_bonus_weight', 0.01, 1000.0)
    transaction_penalty_weight = trial.suggest_loguniform('transaction_penalty_weight', 1e-5, 1e-2)
    holding_bonus_weight = trial.suggest_float('holding_bonus_weight', 0.0, 0.01)
    transaction_penalty_scale = trial.suggest_float('transaction_penalty_scale', 0.5, 2.0)

    volatility_threshold = trial.suggest_float("volatility_threshold", 0.5, 2.0)
    momentum_threshold_min = trial.suggest_float("momentum_threshold_min", 30, 45)
    momentum_threshold_max = trial.suggest_float("momentum_threshold_max", 55, 70)

    hold_threshold = trial.suggest_float("hold_threshold", 0.0, 0.1, step=0.01)
    reward_norm_factor = trial.suggest_float('reward_norm_factor', 0.1, 5.0)
    ema_alpha = trial.suggest_float('ema_alpha', 0.01, 0.2)  # How fast the EMA adapts

    env_train = SingleStockTradingEnv(
        df=df,
        scaler=scaler,
        initial_balance=initial_balance,
        stop_loss=tuned_stop_loss,
        take_profit=tuned_take_profit,
        max_position_size=tuned_max_position_size,
        max_drawdown=tuned_max_drawdown,
        annual_trading_days=annual_trading_days,
        transaction_cost=tuned_transaction_cost,
        env_rank=trial.number + 1,
        some_factor=drawdown_penalty_factor,
        hold_threshold=hold_threshold,
        reward_weights={
            'reward_scale': tuned_reward_scale,
            'profit_weight': profit_weight,
            'sharpe_bonus_weight': sharpe_bonus_weight,
            'transaction_penalty_weight': transaction_penalty_weight,
            'holding_bonus_weight': holding_bonus_weight,
            'transaction_penalty_scale': transaction_penalty_scale,
            'volatility_threshold': volatility_threshold,
            'momentum_threshold_min': momentum_threshold_min,
            'momentum_threshold_max': momentum_threshold_max,
            'reward_norm_factor': reward_norm_factor,
            'ema_alpha': ema_alpha
        }
    )
    env_train.seed(RANDOM_SEED + trial.number + 1)

    vec_env_train = DummyVecEnv([lambda: env_train])

    policy_kwargs = dict(
        activation_fn=torch.nn.ReLU,
        net_arch=[int(x) for x in net_arch.split('_')]
    )

    trial_log_dir = TB_LOG_DIR / f"trial_{trial.number}"
    trial_log_dir.mkdir(parents=True, exist_ok=True)

    model = PPO(
        'MlpPolicy',
        vec_env_train,
        verbose=0,
        seed=RANDOM_SEED,
        policy_kwargs=policy_kwargs,
        learning_rate=learning_rate,
        n_steps=n_steps,
        batch_size=batch_size,
        gamma=gamma,
        gae_lambda=gae_lambda,
        clip_range=clip_range,
        ent_coef=ent_coef,
        vf_coef=vf_coef,
        max_grad_norm=max_grad_norm,
        tensorboard_log=str(trial_log_dir),
        device='cpu'
    )

    trial_checkpoint_dir = RESULTS_DIR / f"checkpoints_trial_{trial.number}"
    trial_checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_callback = CheckpointCallback(
        save_freq=3000,
        save_path=str(trial_checkpoint_dir),
        name_prefix="ppo_model"
    )
    custom_callback = CustomTensorboardCallback()
    early_stopping_callback = EarlyStoppingCallback(
        monitor='train/reward_env',
        patience=3000,
        min_delta=1e-5,
        verbose=1
    )
    callback_list = CallbackList([custom_callback, checkpoint_callback, early_stopping_callback])

    start_time = time.time()
    try:
        model.learn(
            total_timesteps=100000,
            callback=callback_list
        )
    except Exception as e:
        main_logger.critical(f"[Trial {trial.number}] Training failed: {e}")
        return -np.inf
    duration = time.time() - start_time

    env_train_history = env_train.history
    cumulative_reward = sum([entry['Reward'] for entry in env_train_history]) if env_train_history else 0.0

    main_logger.critical(f"[Trial {trial.number}] Cumulative Reward: {cumulative_reward:.4f}")
    main_logger.critical(f"[Trial {trial.number}] Final Net Worth: ${env_train.net_worth:.2f}")
    main_logger.critical(f"[Trial {trial.number}] Final Balance: ${env_train.balance:.2f}")
    main_logger.critical(f"[Trial {trial.number}] Final Position: {env_train.position} shares")
    main_logger.critical(f"[Trial {trial.number}] Total Transactions: {env_train.transaction_count}")
    main_logger.critical(f"[Trial {trial.number}] Final Peak Net Worth: ${env_train.peak:.2f}")
    final_drawdown = (env_train.peak - env_train.net_worth) / env_train.peak if env_train.peak > 0 else 0.0
    main_logger.critical(f"[Trial {trial.number}] Final Drawdown: {final_drawdown*100:.2f}%")

    trial_log_file = RESULTS_DIR / f"trial_{trial.number}_history.csv"
    if env_train_history:
        pd.DataFrame(env_train_history).to_csv(trial_log_file, index=False)
        main_logger.info(f"[Trial {trial.number}] Environment history saved to {trial_log_file}")
    else:
        pd.DataFrame().to_csv(trial_log_file, index=False)
        main_logger.warning(f"[Trial {trial.number}] Environment history was empty. Saved empty CSV at {trial_log_file}")

    return cumulative_reward

##############################################
# Main Execution
##############################################

def make_env(env_params, env_rank, seed=RANDOM_SEED):
    """
    Creates and returns a callable that initializes the SingleStockTradingEnv.

    Args:
        env_params (dict): Parameters to initialize the environment.
        env_rank (int): Unique identifier for the environment.
        seed (int): Random seed.

    Returns:
        callable: A function that creates and returns a SingleStockTradingEnv instance when called.
    """
    def _init():
        env_instance = SingleStockTradingEnv(
            df=env_params['df'],
            scaler=env_params['scaler'],
            initial_balance=env_params['initial_balance'],
            stop_loss=env_params['stop_loss'],
            take_profit=env_params['take_profit'],
            max_position_size=env_params['max_position_size'],
            max_drawdown=env_params['max_drawdown'],
            annual_trading_days=env_params['annual_trading_days'],
            transaction_cost=env_params['transaction_cost'],
            some_factor=env_params['some_factor'],
            env_rank=env_rank,
            reward_weights=env_params.get('reward_weights', None)
        )
        env_instance.seed(seed + env_rank)
        return env_instance
    return _init

if __name__ == "__main__":
    # Define parameters
    TICKER = 'ADVENZYMES.NS'
    CSV_FILE_PATH = r'C:\Users\Star\.cursor-tutor\data\data_fetched.csv'  # <-- Update this path to your CSV file
    START_DATE = '2018-01-01'
    END_DATE = datetime.datetime.now().strftime('%Y-%m-%d')  # Current date
    INITIAL_BALANCE = 100000
    STOP_LOSS = 0.90
    TAKE_PROFIT = 1.10
    MAX_POSITION_SIZE = 0.5  # Increased from 0.25 to 0.5
    MAX_DRAWDOWN = 0.20
    ANNUAL_TRADING_DAYS = 252
    TRANSACTION_COST = 0.001  # 0.1% per trade

    # Fetch and prepare data
    # Scaling is now handled within get_data
    df, scaler = get_data(CSV_FILE_PATH, scaler=None, fit_scaler=True)
    if df.empty:
        main_logger.critical("No data fetched. Exiting.")
        exit()

    # Split into training and testing datasets
    split_ratio = 0.8  # 80% training, 20% testing
    split_idx = int(len(df) * split_ratio)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    test_df = df.iloc[split_idx:].reset_index(drop=True)

    main_logger.info(f"Training data: {len(train_df)} samples")
    main_logger.info(f"Testing data: {len(test_df)} samples")

    # Save train and test data to CSV
    train_data_file = RESULTS_DIR / f"{TICKER}_train_data.csv"
    test_data_file = RESULTS_DIR / f"{TICKER}_test_data.csv"
    train_df.to_csv(train_data_file, index=False)
    test_df.to_csv(test_data_file, index=False)
    main_logger.info(f"Training data saved to {train_data_file}")
    main_logger.info(f"Testing data saved to {test_data_file}")

    # Log test_df size and columns
    main_logger.info(f"Test DataFrame Size: {test_df.shape}")
    main_logger.info(f"Test DataFrame Columns: {test_df.columns.tolist()}")

    # Verify test_df integrity
    if test_df.empty:
        main_logger.critical("Test DataFrame is empty. Exiting.")
        exit()

    # Save the scaler for future use (e.g., deployment)
    scaler_filename = RESULTS_DIR / 'scaler.pkl'
    joblib.dump(scaler, scaler_filename)
    main_logger.info(f"Scaler fitted on training data and saved as {scaler_filename}")

    # Optionally, copy test_df to another variable for baseline strategies
    baseline_test_df = test_df.copy()
    main_logger.info("Copied test_df to baseline_test_df for baseline strategies evaluation.")

    # Check if test_df has sufficient data
    MIN_TEST_SAMPLES = 500  # Define a minimum number of samples for testing
    if len(test_df) < MIN_TEST_SAMPLES:
        main_logger.warning(f"Testing data has only {len(test_df)} samples. Increasing test set size to {MIN_TEST_SAMPLES}.")
        # Adjust split to ensure test_df has at least MIN_TEST_SAMPLES
        split_idx = len(df) - MIN_TEST_SAMPLES
        train_df = df.iloc[:split_idx].reset_index(drop=True)
        test_df = df.iloc[split_idx:].reset_index(drop=True)
        baseline_test_df = test_df.copy()
        main_logger.info(f"Adjusted Training data: {len(train_df)} samples")
        main_logger.info(f"Adjusted Testing data: {len(test_df)} samples")
        main_logger.info(f"Adjusted baseline_test_df Size: {baseline_test_df.shape}")
        main_logger.info(f"Adjusted baseline_test_df Columns: {baseline_test_df.columns.tolist()}")

    # Verify unscaled columns in test_df
    missing_unscaled_test = [feature for feature in UNSCALED_FEATURES if feature not in test_df.columns]
    if missing_unscaled_test:
        main_logger.error(f"Missing unscaled features in test DataFrame: {missing_unscaled_test}")
        exit()
    else:
        main_logger.debug("All unscaled features are present in the test DataFrame.")
        main_logger.debug(f"Columns in test_df: {test_df.columns.tolist()}")

    # Additional Verification: Ensure required columns are present
    required_cols_verification = ['MACD_unscaled', 'Close_unscaled']
    missing_cols_verification = [col for col in required_cols_verification if col not in test_df.columns]
    if missing_cols_verification:
        main_logger.error(f"Missing required columns for MACD strategy in test DataFrame: {missing_cols_verification}")
        # Decide whether to proceed or skip certain strategies
    else:
        main_logger.debug("All required columns for MACD strategy are present in test_df.")

    # Check environment validity
    main_logger.info("Checking environment compatibility with SB3...")
    env_checker = SingleStockTradingEnv(
        df=train_df,
        scaler=scaler,
        initial_balance=INITIAL_BALANCE,
        stop_loss=STOP_LOSS,
        take_profit=TAKE_PROFIT,
        max_position_size=MAX_POSITION_SIZE,
        max_drawdown=MAX_DRAWDOWN,
        annual_trading_days=ANNUAL_TRADING_DAYS,
        transaction_cost=TRANSACTION_COST,
        env_rank=-1  # Assign a default env_rank for the checker
    )
    try:
        check_env(env_checker, warn=True)
        main_logger.info("Environment is valid!")
    except Exception as e:
        main_logger.critical(f"Environment check failed: {e}")
        exit()

    ##############################################
    # Optuna Hyperparameter Tuning
    ##############################################

    # Log Phase: Hyperparameter Tuning Starting
    log_phase("Hyperparameter Tuning", "Starting", {"total_trials": 10})

    # Optuna hyperparameter tuning with limited concurrent trials to prevent overload
    main_logger.info("Starting hyperparameter tuning with Optuna...")

    # Optuna storage using SQLite for persistence
    storage = optuna.storages.RDBStorage(
        url='sqlite:///optuna_study.db',
        engine_kwargs={'connect_args': {'check_same_thread': False}}
    )

    # Generate a unique study name
    unique_study_name = generate_unique_study_name()

    study = optuna.create_study(
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=RANDOM_SEED),
        storage=storage,
        study_name=unique_study_name,  # Unique Study Name
        load_if_exists=False  # Ensure a new study is created
    )
    study.optimize(
    lambda trial: objective(
        trial,
        train_df,
        scaler,
        INITIAL_BALANCE,
        STOP_LOSS,
        TAKE_PROFIT,
        MAX_POSITION_SIZE,
        MAX_DRAWDOWN,
        ANNUAL_TRADING_DAYS,
        TRANSACTION_COST
    ),
    n_trials=10,
    n_jobs=4
    )

    if study.best_params:
        best_params = study.best_params
        main_logger.info(f"Best hyperparameters found: {best_params}")
    else:
        main_logger.critical("No successful trials found in Optuna study.")
        exit()

    # Log Phase: Hyperparameter Tuning Completed
    log_phase("Hyperparameter Tuning", "Completed", {"best_params": best_params})

    ##############################################
    # Main Training
    ##############################################

    # Assign unique env_rank for main training
    main_env_rank = 0  # Unique ID for main training

    # Create environment parameters with best reward weights
    env_params = {
        'df': train_df,
        'scaler': scaler,
        'initial_balance': INITIAL_BALANCE,
        'stop_loss': best_params.get('stop_loss', STOP_LOSS),
        'take_profit': best_params.get('take_profit', TAKE_PROFIT),
        'max_position_size': best_params.get('max_position_size', MAX_POSITION_SIZE),
        'max_drawdown': best_params.get('max_drawdown', MAX_DRAWDOWN),
        'annual_trading_days': ANNUAL_TRADING_DAYS,
        'transaction_cost': best_params.get('transaction_cost', TRANSACTION_COST),
        'some_factor': best_params.get('drawdown_penalty_factor', 0.01),
        'reward_weights': {
            'reward_scale': best_params.get('reward_scale', 1.0),
            'profit_weight': best_params.get('profit_weight', 1.0),
            'sharpe_bonus_weight': best_params.get('sharpe_bonus_weight', 0.05),
            'transaction_penalty_weight': best_params.get('transaction_penalty_weight', 1e-3),
            'holding_bonus_weight': best_params.get('holding_bonus_weight', 0.001),
            'transaction_penalty_scale': best_params.get('transaction_penalty_scale', 1.0),
            'volatility_threshold': best_params.get('volatility_threshold', 1.0),
            'momentum_threshold_min': best_params.get('momentum_threshold_min', 30.0),
            'momentum_threshold_max': best_params.get('momentum_threshold_max', 70.0),
            'reward_norm_factor': best_params.get('reward_norm_factor', 1.0),
            'ema_alpha': best_params.get('ema_alpha', 0.01)
        }
    }


    # Log Phase: Main Training Starting
    log_phase("Main Training", "Starting", {"env_rank": main_env_rank, "total_timesteps": 500000, "reward_weights": env_params['reward_weights']})

    # Initialize training environment
    vec_env_train = DummyVecEnv([make_env(env_params, main_env_rank, RANDOM_SEED)])

    main_logger.info(f"Initialized DummyVecEnv with env_rank={main_env_rank} for main training.")

    # Define policy kwargs
    policy_kwargs = dict(
        activation_fn=torch.nn.ReLU,
        net_arch=[int(x) for x in best_params.get('net_arch', '128_128').split('_')]
    )

    # Define unique TensorBoard log directory for main training
    main_training_log_dir = TB_LOG_DIR / f"main_training_{main_env_rank}"
    main_training_log_dir.mkdir(parents=True, exist_ok=True)

    # Initialize PPO model with best hyperparameters and enable CPU usage
    try:
        model = PPO(
            'MlpPolicy',
            vec_env_train,
            verbose=1,  # Set verbose to 1 to enable logging
            seed=RANDOM_SEED,
            policy_kwargs=policy_kwargs,
            learning_rate=best_params.get('learning_rate', 3e-4),
            n_steps=best_params.get('n_steps', 128),
            batch_size=best_params.get('batch_size', 64),
            gamma=best_params.get('gamma', 0.99),
            gae_lambda=best_params.get('gae_lambda', 0.95),
            clip_range=best_params.get('clip_range', 0.2),
            ent_coef=best_params.get('ent_coef', 0.02),  # Increased entropy coefficient for exploration
            vf_coef=best_params.get('vf_coef', 0.5),
            max_grad_norm=best_params.get('max_grad_norm', 0.5),
            tensorboard_log=str(main_training_log_dir),
            device='cpu'  # Use CPU
        )
    except Exception as e:
        main_logger.critical(f"Model initialization failed: {e}")
        exit()

    # Define checkpoint and custom callbacks
    checkpoint_callback = CheckpointCallback(
        save_freq=50000,  # Reduced frequency to prevent overload
        save_path=str(RESULTS_DIR / "checkpoints"),
        name_prefix="ppo_model"
    )
    custom_callback = CustomTensorboardCallback()
    early_stopping_callback = EarlyStoppingCallback(
        monitor='train/reward_env',
        patience=3000,  # Increased patience
        min_delta=1e-5,  # Lowered min_delta
        verbose=1
    )

    # Create a CallbackList
    callback_list = CallbackList([custom_callback, checkpoint_callback, early_stopping_callback])

    # Start training
    start_time = time.time()
    try:
        model.learn(
            total_timesteps=500000,  # Increased training steps
            callback=callback_list
        )
    except Exception as e:
        main_logger.critical(f"Training failed: {e}")
        exit()
    duration = time.time() - start_time

    # Log Phase: Main Training Completed
    log_phase("Main Training", "Completed", {"env_rank": main_env_rank, "total_timesteps": 500000}, duration)

    # Save the trained model
    model_path = RESULTS_DIR / f"ppo_model_{TICKER}.zip"
    model.save(str(model_path))
    main_logger.info(f"Model trained and saved at {model_path}")

    ##############################################
    # Testing
    ##############################################

    # Log Phase: Testing Starting
    log_phase("Testing Phase", "Starting", {"env_rank": 999, "data_points": len(test_df)})

    main_logger.info("Starting testing of PPO agent...")

    # Assign unique env_rank for testing
    test_env_rank = 999  # Unique ID for testing

    # Initialize evaluation environment (separate from training) without vectorization    
    env_test = SingleStockTradingEnv(
        df=test_df,
        scaler=scaler,
        initial_balance=INITIAL_BALANCE,
        stop_loss=best_params.get('stop_loss', STOP_LOSS),
        take_profit=best_params.get('take_profit', TAKE_PROFIT),
        max_position_size=best_params.get('max_position_size', MAX_POSITION_SIZE),
        max_drawdown=best_params.get('max_drawdown', MAX_DRAWDOWN),
        annual_trading_days=ANNUAL_TRADING_DAYS,
        transaction_cost=best_params.get('transaction_cost', TRANSACTION_COST),
        some_factor=best_params.get('drawdown_penalty_factor', 0.01),
        env_rank=test_env_rank,
        reward_weights={
            'reward_scale': best_params.get('reward_scale', 1.0),
            'profit_weight': best_params.get('profit_weight', 1.0),
            'sharpe_bonus_weight': best_params.get('sharpe_bonus_weight', 0.05),
            'transaction_penalty_weight': best_params.get('transaction_penalty_weight', 1e-3),
            'holding_bonus_weight': best_params.get('holding_bonus_weight', 0.001),
            'transaction_penalty_scale': best_params.get('transaction_penalty_scale', 1.0),
            'volatility_threshold': best_params.get('volatility_threshold', 1.0),
            'momentum_threshold_min': best_params.get('momentum_threshold_min', 30.0),
            'momentum_threshold_max': best_params.get('momentum_threshold_max', 70.0),
            'reward_norm_factor': best_params.get('reward_norm_factor', 1.0),
            'ema_alpha': best_params.get('ema_alpha', 0.01)
        }
    )

    env_test.seed(RANDOM_SEED + test_env_rank)

    main_logger.info(f"Initialized SingleStockTradingEnv with env_rank={test_env_rank} for testing.")

    # Reset the environment
    try:
        obs, info = env_test.reset()
        main_logger.info(f"Environment reset successfully. Starting steps.")
    except Exception as e:
        main_logger.critical(f"Reset failed during testing: {e}")
        exit()

    done = False
    steps_taken = 0
    max_test_steps = len(test_df)  # Prevent infinite loops

    # Determine steps for first, middle, and last
    first_step = 0
    middle_step = max_test_steps // 2
    last_step = max_test_steps - 1

    # Initialize history DataFrame
    rl_test_history = []

    while not done and steps_taken < max_test_steps:
        try:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, done, truncated, info = env_test.step(action)
            steps_taken += 1

            # Log first, middle, and last steps
            if steps_taken in [first_step, middle_step, last_step]:
                testing_logger.info(f"[Test Env {test_env_rank}] Step {steps_taken}: Action Taken = {action}, Reward = {reward}")

            # Append to RL test history
            if hasattr(env_test, 'history') and len(env_test.history) >= steps_taken:
                rl_test_history.append(env_test.history[-1])

        except Exception as e:
            main_logger.critical(f"Step failed during testing: {e}")
            break

    duration = time.time() - start_time
    # Log Phase: Testing Completed
    log_phase("Testing Phase", "Completed", {"env_rank": test_env_rank, "data_points": len(test_df), "steps_taken": steps_taken}, duration)

    # Create DataFrame for RL Agent Performance from testing environment history
    if rl_test_history:
        rl_test_df = pd.DataFrame(rl_test_history)
        main_logger.info(f"Testing environment history has {len(rl_test_df)} entries.")
    else:
        main_logger.error("Testing environment does not have a 'history' attribute or it's empty.")
        rl_test_df = pd.DataFrame()

    # Ensure 'Reward' column exists
    if 'Reward' not in rl_test_df.columns:
        rl_test_df['Reward'] = 0.0
        main_logger.critical("'Reward' column not found in RL test history. Defaulting rewards to 0.")

    # Calculate Performance Metrics for Testing
    if not rl_test_df.empty:
        # Correct the column name from 'net worth' to 'Net Worth'
        if 'Net Worth' in rl_test_df.columns:
            test_final_net_worth = float(rl_test_df['Net Worth'].iloc[-1])  # Ensure float
        elif 'net worth' in rl_test_df.columns:
            test_final_net_worth = float(rl_test_df['net worth'].iloc[-1])
        else:
            main_logger.error("Neither 'Net Worth' nor 'net worth' column found in RL test history.")
            test_final_net_worth = INITIAL_BALANCE

        test_profit = float(test_final_net_worth - INITIAL_BALANCE)  # Ensure float
        if 'Net Worth' in rl_test_df.columns or 'net worth' in rl_test_df.columns:
            test_max_dd = calculate_max_drawdown(rl_test_df['Net Worth']) if 'Net Worth' in rl_test_df.columns else calculate_max_drawdown(rl_test_df['net worth'])
            test_annualized_return = calculate_annualized_return(rl_test_df['Net Worth']) if 'Net Worth' in rl_test_df.columns else calculate_annualized_return(rl_test_df['net worth'])
        else:
            test_max_dd = 0.0
            test_annualized_return = 0.0
    else:
        test_final_net_worth = INITIAL_BALANCE
        test_profit = 0.0
        test_max_dd = 0.0
        test_annualized_return = 0.0

    # Calculate Transaction Costs
    rl_transaction_cost = rl_test_df['Trade_Cost'].sum()
    num_buys = (rl_test_df['Action'] > 0).sum()
    num_sells = (rl_test_df['Action'] < 0).sum()
    rl_transaction_count = num_buys + num_sells    

    # Save Test Environment History to CSV
    test_history_file = RESULTS_DIR / "test_env_history.csv"
    if not rl_test_df.empty:
        rl_test_df.to_csv(test_history_file, index=False)
        main_logger.info(f"Testing environment history saved to {test_history_file}")
    else:
        # Save an empty DataFrame or with default values if history is empty
        pd.DataFrame().to_csv(test_history_file, index=False)
        main_logger.warning(f"Testing environment history was empty. Saved empty CSV at {test_history_file}")

    ##############################################
    # Evaluate Baseline Strategies on Test Data
    ##############################################

    main_logger.info("Evaluating Baseline Strategies on Test Data...")

    # Verify that baseline_test_df has all required columns
    required_baseline_cols = ['MACD_unscaled', 'Close_unscaled', 'SMA10_unscaled', 'SMA50_unscaled',
                              'BB_Upper_unscaled', 'BB_Lower_unscaled']
    missing_baseline_cols = [col for col in required_baseline_cols if col not in baseline_test_df.columns]
    if missing_baseline_cols:
        main_logger.error(f"Missing columns in baseline_test_df required for baseline strategies: {missing_baseline_cols}")
        # Decide whether to proceed or skip certain strategies
    else:
        main_logger.debug("All required columns for baseline strategies are present in baseline_test_df.")

    # Evaluate Buy and Hold Strategy
    bh_result, bh_history = buy_and_hold_with_iloc(baseline_test_df, initial_balance=INITIAL_BALANCE, transaction_cost=TRANSACTION_COST)

    # Evaluate MACD Strategy
    if 'MACD_unscaled' in baseline_test_df.columns and 'Close_unscaled' in baseline_test_df.columns:
        macd_result, macd_history = macd_strategy_with_iloc(baseline_test_df, initial_balance=INITIAL_BALANCE, transaction_cost=TRANSACTION_COST, max_position_size=MAX_POSITION_SIZE)
    else:
        main_logger.warning("Skipping MACD strategy due to missing 'MACD_unscaled' or 'Close_unscaled' in baseline_test_df.")
        macd_result, macd_history = {
            'Strategy': 'MACD Crossover',
            'Initial Balance': INITIAL_BALANCE,
            'Final Net Worth': 0.0,
            'Profit': 0.0
        }, pd.DataFrame()

    # Evaluate Moving Average Crossover Strategy
    if all(col in baseline_test_df.columns for col in ['SMA10_unscaled', 'SMA50_unscaled', 'Close_unscaled']):
        ma_crossover_result, ma_crossover_history = moving_average_crossover_with_iloc(baseline_test_df, initial_balance=INITIAL_BALANCE, transaction_cost=TRANSACTION_COST, max_position_size=MAX_POSITION_SIZE)
    else:
        main_logger.warning("Skipping Moving Average Crossover strategy due to missing required columns in baseline_test_df.")
        ma_crossover_result, ma_crossover_history = {
            'Strategy': 'Moving Average Crossover',
            'Initial Balance': INITIAL_BALANCE,
            'Final Net Worth': 0.0,
            'Profit': 0.0
        }, pd.DataFrame()

    # Evaluate Bollinger Bands Strategy
    if all(col in baseline_test_df.columns for col in ['BB_Upper_unscaled', 'BB_Lower_unscaled', 'Close_unscaled']):
        bb_result, bb_history = bollinger_bands_strategy_with_iloc(baseline_test_df, initial_balance=INITIAL_BALANCE, transaction_cost=TRANSACTION_COST, max_position_size=MAX_POSITION_SIZE)
    else:
        main_logger.warning("Skipping Bollinger Bands strategy due to missing required columns in baseline_test_df.")
        bb_result, bb_history = {
            'Strategy': 'Bollinger Bands',
            'Initial Balance': INITIAL_BALANCE,
            'Final Net Worth': 0.0,
            'Profit': 0.0
        }, pd.DataFrame()

    # Evaluate Random Strategy
    random_result, random_history = random_strategy_with_iloc(baseline_test_df, initial_balance=INITIAL_BALANCE, transaction_cost=TRANSACTION_COST, max_position_size=MAX_POSITION_SIZE)

    # Collect all baseline results and histories
    baseline_results = [
        (bh_result, bh_history),
        (macd_result, macd_history),
        (ma_crossover_result, ma_crossover_history),
        (bb_result, bb_history),
        (random_result, random_history)
    ]

    # Log Baseline Results
    for result, history_df in baseline_results:
        main_logger.critical(f"Strategy: {result['Strategy']}")
        main_logger.critical(f"  Initial Balance: ${result['Initial Balance']}")
        main_logger.critical(f"  Final Net Worth: ${result['Final Net Worth']:.2f}")
        main_logger.critical(f"  Profit: ${result['Profit']:.2f}")
        if not history_df.empty:
            buy_count = history_df['Action'].value_counts().get('Buy', 0)
            sell_count = history_df['Action'].value_counts().get('Sell', 0)
            main_logger.critical(f"  Total Transactions: {buy_count + sell_count}")
        else:
            main_logger.critical(f"  Total Transactions: 0")
        main_logger.critical("-" * 50)

    # Log and print RL Agent Results on Test Data
    main_logger.critical("RL Agent Performance on Test Data:")
    main_logger.critical(f"  Final Net Worth: ${test_final_net_worth:.2f}")
    main_logger.critical(f"  Profit: ${test_profit:.2f}")
    main_logger.critical(f"  Annualized Return: {test_annualized_return*100:.2f}%")
    main_logger.critical(f"  Max Drawdown: {test_max_dd*100:.2f}%")
    main_logger.critical(f"  Transaction Costs: ${rl_transaction_cost:.2f}")
    main_logger.critical(f"  Transaction Count: {int(rl_transaction_count)}")
    main_logger.critical("-" * 50)

    ##############################################
    # Plotting All Results to PDF
    ##############################################

    pdf_path = RESULTS_DIR / "trading_results_plots.pdf"
    try:
        with PdfPages(pdf_path) as pdf:
            # Plot RL Training History
            training_history = pd.DataFrame(vec_env_train.envs[0].history) if hasattr(vec_env_train.envs[0], 'history') else pd.DataFrame()
            plot_rl_training_history(training_history, pdf)

            # Plot Buy and Sell Signals for Each Strategy
            strategy_history = {
                'RL Strategy': rl_test_df,
                'Buy and Hold': bh_history,
                'MACD Crossover': macd_history,
                'Moving Average Crossover': ma_crossover_history,
                'Bollinger Bands': bb_history,
                'Random Strategy': random_history
            }

            plot_all_buy_sell_signals(strategy_history, pdf)

            # Plot Transaction Costs
            plot_transaction_costs(baseline_results, pdf)

            # Plot Final Net Worth
            plot_cash_balance(baseline_results, pdf)

            # Plot Transaction Count
            plot_transaction_count(baseline_results, pdf)

            # Plot Reward Movements for RL Strategy
            plot_reward_movements(rl_test_df, pdf)

            # Plot Position Movements for RL Strategy
            plot_position_movements(rl_test_df, pdf)

            # Plot Drawdown Movements for RL Strategy
            plot_drawdown_movements(rl_test_df, pdf)

            # Plot Profit Comparison Among Strategies
            plot_profit_comparison(baseline_results, pdf)

            # Plot RL Agent vs Baseline Strategies
            plot_comparison(test_df, rl_test_df, baseline_results, INITIAL_BALANCE, TICKER, pdf)
        
        main_logger.critical(f"All plots have been saved to {pdf_path}")
    except Exception as e:
        main_logger.error(f"Error during PDF generation: {e}")

    ##############################################
    # Instructions for TensorBoard
    ##############################################

    main_logger.critical("Training logs are stored for TensorBoard.")
    main_logger.critical("To view them, run the following command in your terminal:")
    main_logger.critical(f"tensorboard --logdir {TB_LOG_DIR}")
    main_logger.critical("Then open http://localhost:6006 in your browser to visualize the training metrics.")

    ##############################################
    # Final Logging and Cleanup
    ##############################################

    # Log Phase: All Phases Completed
    total_duration = time.time() - start_time
    log_phase("All Phases", "Completed", {"total_duration_seconds": total_duration}, total_duration)

    main_logger.info("Script execution completed successfully.")
