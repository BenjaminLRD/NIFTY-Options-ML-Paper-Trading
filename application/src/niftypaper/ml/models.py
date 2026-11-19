"""The model registry. A model here is a small, fully inspectable function from one feature to a probability that the contract's next settlement is above its
current one. No model is trained, tuned or refitted by this program, and none is shown to be profitable.

Models and their standing (the standing is what the decision layer reads; see docs/ML_INTEGRATION.md):

b0_training_prior      The research baseline B0: the training-period up-rate, a constant per option type. The two constants are the training up-rates printed in the
                       research folder's V5_1_Final_Research_Report.md (CE 0.4421, PE 0.4795). It does not depend on the data, so it can never ask for a trade.
b1_lr1_logistic        Research model B1 (feature lr1). Same functional form as the research (winsorize at the training 1st/99th percentile, standardize, logistic).
b2_ur1_logistic        Research model B2 (feature ur1). The fitted numbers were not saved by the research code, so this program has none: it needs a parameter file
                       (config/local/model_params/<model id>.json, schema below) that the owner exports from the frozen research code. Without one it reports
                       PARAMETERS_NOT_AVAILABLE and predicts nothing. Both have standing NONE: the V5.1 evaluation did not find them better than the prior.
demo_synthetic_ur1     A DEMONSTRATION model with hand-set numbers, fitted to nothing. Its only purpose is to exercise the pipeline on invented data. It is
                       restricted to SYNTHETIC_FIXTURE inputs and carries no research standing.

Standing NONE: the model may be displayed but the decision layer never trades on it. DEMONSTRATION_ONLY: it may drive simulated trades on synthetic fixture data.

Parameter file schema "niftypaper.model_params/1": {"schema", "model_id", "trained_through" (YYYY-MM-DD), "source_note", "by_option_type": {"CE": P, "PE": P}} with
P = {"lo", "hi", "mean", "sd", "intercept", "coef"} (finite numbers, lo <= hi, sd > 0). Unknown keys are refused.
"""
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import features

SYNTHETIC = "SYNTHETIC_FIXTURE"
NONE_STANDING, DEMO_STANDING = "NONE", "DEMONSTRATION_ONLY"
PARAMS_OK, PARAMS_MISSING, PARAMS_INVALID = "AVAILABLE", "PARAMETERS_NOT_AVAILABLE", "PARAMETERS_INVALID"
PARAMS_SCHEMA = "niftypaper.model_params/1"
DEFAULT_PARAMS_DIR = Path(__file__).resolve().parents[3] / "config" / "local" / "model_params"
_KEYS = ("lo", "hi", "mean", "sd", "intercept", "coef")

# Training up-rates from the research report (section 6.2). The evaluation-window up-rates in the same table are deliberately NOT used.
PRIOR_RATES = {"CE": 0.4421, "PE": 0.4795}


@dataclass(frozen=True)
class Params:
    lo: float
    hi: float
    mean: float
    sd: float
    intercept: float
    coef: float

    def __post_init__(self):
        values = (self.lo, self.hi, self.mean, self.sd, self.intercept, self.coef)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values) or self.lo > self.hi or self.sd <= 0:
            raise ValueError("parameters must be finite numbers with lo <= hi and sd > 0")

    def probability(self, value):
        x = min(max(value, self.lo), self.hi)                   # winsorize, then standardize, exactly as the research transform does
        z = self.intercept + self.coef * (x - self.mean) / self.sd
        return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


@dataclass(frozen=True)
class ModelCard:
    model_id: str
    kind: str                   # "prior" or "logistic"
    feature: "str | None"
    standing: str               # NONE or DEMONSTRATION_ONLY
    allowed_origins: tuple      # origin classes the model may be used on
    description: str
    evidence: str               # what is known about its quality, in words
    params_status: str
    params_note: str = ""


class Model:
    def __init__(self, card, by_option_type=None):
        self.card, self._params = card, by_option_type or {}

    @property
    def model_id(self):
        return self.card.model_id

    def probability(self, option_type, value):
        """Probability that the contract's next settlement is above its current one, or None if this model cannot say."""
        if self.card.kind == "prior":
            return PRIOR_RATES.get(option_type)
        params = self._params.get(option_type)
        return None if params is None or value is None else params.probability(value)


def _card(model_id, kind, feature, standing, origins, description, evidence, status, note=""):
    return ModelCard(model_id, kind, feature, standing, tuple(origins), description, evidence, status, note)


RESEARCH_EVIDENCE = ("V5.1 one-shot evaluation (2026-06-30 to 2026-09-28): Brier difference against the prior was positive or about zero for every option type "
                     "and the interval was not below 0 (NOT SUPPORTED). No profitability claim exists.")
# Hand-set, fitted to nothing: a gentle trend-following slope on the index return for calls and the opposite for puts (a put gains when the index falls).
DEMO_PARAMS = {"CE": Params(-0.05, 0.05, 0.0, 0.01, 0.0, 1.0), "PE": Params(-0.05, 0.05, 0.0, 0.01, 0.0, -1.0)}


def load_params_file(path, model_id):
    """(by_option_type, note) from a parameter file, or (None, reason). Never raises for a bad file: a bad file simply means no parameters."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"schema", "model_id", "trained_through", "source_note", "by_option_type"}:
            raise ValueError("unexpected keys")
        if raw["schema"] != PARAMS_SCHEMA or raw["model_id"] != model_id:
            raise ValueError("schema or model id does not match")
        date.fromisoformat(raw["trained_through"])
        if not isinstance(raw["source_note"], str) or not isinstance(raw["by_option_type"], dict) or not set(raw["by_option_type"]) <= {"CE", "PE"}:
            raise ValueError("bad fields")
        parsed = {}
        for option_type, body in raw["by_option_type"].items():
            if not isinstance(body, dict) or set(body) != set(_KEYS):
                raise ValueError("bad parameter block")
            parsed[option_type] = Params(**body)
        if not parsed:
            raise ValueError("no parameters")
        return parsed, f"trained through {raw['trained_through']}: {raw['source_note']}"
    except (OSError, ValueError, TypeError, KeyError) as error:
        return None, f"{error.__class__.__name__}: {error}"


def build_registry(params_dir=DEFAULT_PARAMS_DIR):
    """Every model the program knows, keyed by id. A research model without a valid parameter file is registered with status PARAMETERS_NOT_AVAILABLE
    (or PARAMETERS_INVALID) and predicts nothing."""
    registry = {}
    registry["b0_training_prior"] = Model(_card(
        "b0_training_prior", "prior", None, NONE_STANDING, ("*",), "Constant training up-rate per option type (research baseline B0).",
        "It is the benchmark the research compared against; it carries no information about the next move.", PARAMS_OK,
        "constants from the research report, training period only"))
    for model_id, feature in (("b1_lr1_logistic", features.LR1), ("b2_ur1_logistic", features.UR1)):
        path = Path(params_dir) / f"{model_id}.json"
        if not path.exists():
            card = _card(model_id, "logistic", feature, NONE_STANDING, ("*",), f"Research logistic model on {feature}.", RESEARCH_EVIDENCE, PARAMS_MISSING,
                         f"no parameter file at {path.name}; the research code did not save fitted numbers")
            registry[model_id] = Model(card)
            continue
        params, note = load_params_file(path, model_id)
        status = PARAMS_OK if params else PARAMS_INVALID
        registry[model_id] = Model(_card(model_id, "logistic", feature, NONE_STANDING, ("*",), f"Research logistic model on {feature}.", RESEARCH_EVIDENCE,
                                         status, note), params)
    registry["demo_synthetic_ur1"] = Model(_card(
        "demo_synthetic_ur1", "logistic", features.UR1, DEMO_STANDING, (SYNTHETIC,),
        "Pipeline demonstration with hand-set numbers (trend slope on the index return; opposite for puts).",
        "Fitted to no data. Its output on invented data is arbitrary and says nothing about any market.", PARAMS_OK, "hand-set constants in the source code"), DEMO_PARAMS)
    return registry
