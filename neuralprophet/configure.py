from collections import OrderedDict
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import logging
import inspect
import torch
import math

log = logging.getLogger("NP.config")


def from_kwargs(cls, kwargs):
    return cls(**{k: v for k, v in kwargs.items() if k in inspect.signature(cls).parameters})


@dataclass
class Trend:
    growth: str
    changepoints: (list, np.array)
    n_changepoints: int
    changepoints_range: float
    trend_reg: float
    trend_reg_threshold: (bool, float)
    trend_cap_user: bool = False
    trend_floor_user: bool = False

    def __post_init__(self):
        # check validity of setting
        if self.growth not in ["off", "linear", "discontinuous", "logistic"]:
            if self.growth == True:
                self.growth = "linear"
                log.info("Trend growth set to '{}'".format(self.growth))
            elif self.growth == False:
                self.growth = "off"
                log.info("Trend growth set to '{}'".format(self.growth))
            else:
                log.error("Invalid trend growth '{}'. Default to 'linear'".format(self.growth))
                self.growth = "linear"

        if self.growth == "off":
            self.changepoints = None
            self.n_changepoints = 0

        elif self.growth == "logistic":
            # scale parameter of trend delta initialization
            self.tau = 0.1
            # quantiles for initializing the floor and cap of the logistic growth model for robustness against outliers
            # only used in init train loader (access to training data is required)
            self.trend_floor_init_quantile = 0.1
            self.trend_cap_init_quantile = 0.9
            # will be overwritten by init_logistic_growth:
            self.initial_slope = 0.0
            self.cap_quantile = torch.Tensor([0.5])
            self.floor_quantile = torch.Tensor([-0.5])

        # custom changepoints
        if self.changepoints is not None:
            self.n_changepoints = len(self.changepoints)
            self.changepoints = pd.to_datetime(self.changepoints).values

        # handle trend_reg_threshold
        if type(self.trend_reg_threshold) == bool:
            if self.trend_reg_threshold:
                self.trend_reg_threshold = 3.0 / (3.0 + (1.0 + self.trend_reg) * np.sqrt(self.n_changepoints))
                log.debug("Trend reg threshold automatically set to: {}".format(self.trend_reg_threshold))
            else:
                self.trend_reg_threshold = None
        elif self.trend_reg_threshold < 0:
            log.warning("Negative trend reg threshold set to zero.")
            self.trend_reg_threshold = None
        elif math.isclose(self.trend_reg_threshold, 0):
            self.trend_reg_threshold = None

        # handle trend_reg
        if self.trend_reg < 0:
            log.warning("Negative trend reg lambda set to zero.")
            self.trend_reg = 0
        if self.trend_reg > 0:
            if self.n_changepoints > 0:
                log.info("Note: Trend changepoint regularization is experimental.")
                self.trend_reg = 0.001 * self.trend_reg
            else:
                log.info("Trend reg lambda ignored due to no changepoints.")
                self.trend_reg = 0
                if self.trend_reg_threshold > 0:
                    log.info("Trend reg threshold ignored due to no changepoints.")
        else:
            if self.trend_reg_threshold is not None and self.trend_reg_threshold > 0:
                log.info("Trend reg threshold ignored due to reg lambda <= 0.")

    def init_logistic_growth(self, dataset):
        """initialize logistic growth model base rate, cap, and floor with information from training dataset
            Gives more robust training in common cases
        Args:
            dataset (TimeDataset)
        Returns:
            nothing
        """
        assert self.growth == "logistic"
        # initialize base rate k0 with linear slope to give correct initial sign of trend rate in overall logistic curve
        (slope, bias), _, _, _ = np.linalg.lstsq(
            np.concatenate(
                [
                    np.array(dataset.inputs["time"]),
                    np.ones((dataset.inputs["time"].shape[0], 1)),
                ],
                axis=1,
            ),
            dataset.targets,
            rcond=None,
        )
        self.initial_slope = slope

        # ceiling or carrying capacity of logistic growth trend
        if not self.trend_cap_user:
            self.cap_quantile = torch.Tensor(
                torch.kthvalue(
                    dataset.targets.squeeze(),
                    int(dataset.targets.shape[0] * self.trend_cap_init_quantile),
                )
            )[0]

        # floor or lowest point of logistic growth trend
        if not self.trend_cap_user:
            self.floor_quantile = torch.Tensor(
                torch.kthvalue(
                    dataset.targets.squeeze(),
                    int(dataset.targets.shape[0] * self.trend_floor_init_quantile),
                )
            )[0]


@dataclass
class Season:
    resolution: int
    period: float
    arg: str


@dataclass
class AllSeason:
    mode: str = "additive"
    computation: str = "fourier"
    reg_lambda: float = 0
    yearly_arg: (str, bool, int) = "auto"
    weekly_arg: (str, bool, int) = "auto"
    daily_arg: (str, bool, int) = "auto"
    periods: OrderedDict = field(init=False)  # contains SeasonConfig objects

    def __post_init__(self):
        if self.reg_lambda > 0 and self.computation == "fourier":
            log.info("Note: Fourier-based seasonality regularization is experimental.")
            self.reg_lambda = 0.01 * self.reg_lambda
        self.periods = OrderedDict(
            {
                "yearly": Season(resolution=6, period=365.25, arg=self.yearly_arg),
                "weekly": Season(resolution=3, period=7, arg=self.weekly_arg),
                "daily": Season(resolution=6, period=1, arg=self.daily_arg),
            }
        )

    def append(self, name, period, resolution, arg):
        self.periods[name] = Season(resolution=resolution, period=period, arg=arg)

    def set_auto_seasonalities(self, dates):
        """Set seasonalities that were left on auto or set by user.

        Turns on yearly seasonality if there is >=2 years of history.
        Turns on weekly seasonality if there is >=2 weeks of history, and the
        spacing between dates in the history is <7 days.
        Turns on daily seasonality if there is >=2 days of history, and the
        spacing between dates in the history is <1 day.

        Args:
            dates (pd.Series): datestamps
            season_config (configure.AllSeason): NeuralProphet seasonal model configuration, as after __init__
        Returns:
            season_config (configure.AllSeason): processed NeuralProphet seasonal model configuration

        """
        log.debug("seasonality config received: {}".format(self))
        first = dates.min()
        last = dates.max()
        dt = dates.diff()
        min_dt = dt.iloc[dt.values.nonzero()[0]].min()
        auto_disable = {
            "yearly": last - first < pd.Timedelta(days=730),
            "weekly": ((last - first < pd.Timedelta(weeks=2)) or (min_dt >= pd.Timedelta(weeks=1))),
            "daily": ((last - first < pd.Timedelta(days=2)) or (min_dt >= pd.Timedelta(days=1))),
        }
        for name, period in self.periods.items():
            arg = period.arg
            default_resolution = period.resolution
            if arg == "custom":
                continue
            elif arg == "auto":
                resolution = 0
                if auto_disable[name]:
                    log.info(
                        "Disabling {name} seasonality. Run NeuralProphet with "
                        "{name}_seasonality=True to override this.".format(name=name)
                    )
                else:
                    resolution = default_resolution
            elif arg is True:
                resolution = default_resolution
            elif arg is False:
                resolution = 0
            else:
                resolution = int(arg)
            self.periods[name].resolution = resolution

        new_periods = OrderedDict({})
        for name, period in self.periods.items():
            if period.resolution > 0:
                new_periods[name] = period
        self.periods = new_periods
        season_config = self if len(self.periods) > 0 else None
        log.debug("seasonality config: {}".format(season_config))
        return season_config


@dataclass
class Train:
    learning_rate: (float, None)
    epochs: (int, None)
    batch_size: (int, None)
    loss_func: (str, torch.nn.modules.loss._Loss)
    train_speed: (int, float, None)
    ar_sparsity: (float, None)
    reg_delay_pct: float = 0.5
    reg_lambda_trend: float = None
    trend_reg_threshold: (bool, float) = None
    reg_lambda_season: float = None

    def __post_init__(self):
        if self.epochs is not None:
            self.lambda_delay = int(self.reg_delay_pct * self.epochs)
        if type(self.loss_func) == str:
            if self.loss_func.lower() in ["huber", "smoothl1", "smoothl1loss"]:
                self.loss_func = torch.nn.SmoothL1Loss()
            elif self.loss_func.lower() in ["mae", "l1", "l1loss"]:
                self.loss_func = torch.nn.L1Loss()
            elif self.loss_func.lower() in ["mse", "mseloss", "l2", "l2loss"]:
                self.loss_func = torch.nn.MSELoss()
            else:
                raise NotImplementedError("Loss function {} name not defined".format(self.loss_func))
        elif hasattr(torch.nn.modules.loss, self.loss_func.__class__.__name__):
            pass
        else:
            raise NotImplementedError("Loss function {} not found".format(self.loss_func))

    def set_auto_batch_epoch(
        self,
        n_data: int,
        min_batch: int = 1,
        max_batch: int = 128,
        min_epoch: int = 5,
        max_epoch: int = 1000,
    ):
        assert n_data >= 1
        log_data = int(np.log10(n_data))
        if self.batch_size is None:
            log2_batch = 2 * log_data - 1
            self.batch_size = 2 ** log2_batch
            self.batch_size = min(max_batch, max(min_batch, self.batch_size))
            log.info("Auto-set batch_size to {}".format(self.batch_size))
        if self.epochs is None:
            datamult = 1000.0 / float(n_data)
            self.epochs = int(datamult * (2 ** (3 + log_data)))
            self.epochs = min(max_epoch, max(min_epoch, self.epochs))
            log.info("Auto-set epochs to {}".format(self.epochs))
            # also set lambda_delay:
            self.lambda_delay = int(self.reg_delay_pct * self.epochs)

    def apply_train_speed(self, batch=False, epoch=False, lr=False):
        if self.train_speed is not None and not math.isclose(self.train_speed, 0):
            if batch:
                self.batch_size = max(1, int(self.batch_size * 2 ** self.train_speed))
                log.info(
                    "train_speed-{} {}creased batch_size to {}".format(
                        self.train_speed, ["in", "de"][int(self.train_speed < 0)], self.batch_size
                    )
                )
            if epoch:
                self.epochs = max(1, int(self.epochs * 2 ** -self.train_speed))
                log.info(
                    "train_speed-{} {}creased epochs to {}".format(
                        self.train_speed, ["in", "de"][int(self.train_speed > 0)], self.epochs
                    )
                )
            if lr:
                self.learning_rate = self.learning_rate * 2 ** self.train_speed
                log.info(
                    "train_speed-{} {}creased learning_rate to {}".format(
                        self.train_speed, ["in", "de"][int(self.train_speed < 0)], self.learning_rate
                    )
                )

    def apply_train_speed_all(self):
        if self.train_speed is not None and not math.isclose(self.train_speed, 0):
            self.apply_train_speed(batch=True, epoch=True, lr=True)


@dataclass
class Model:
    num_hidden_layers: int
    d_hidden: int


@dataclass
class Covar:
    reg_lambda: float
    as_scalar: bool
    normalize: (bool, str)

    def __post_init__(self):
        if self.reg_lambda is not None:
            if self.reg_lambda < 0:
                raise ValueError("regularization must be >= 0")
