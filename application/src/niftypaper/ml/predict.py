"""The prediction interface: timestamped input in, one Prediction out.

    predict(model, view, contract) -> Prediction

`view` is a historical.gate.PointInTimeView, so the input is "everything available at view.now and nothing later". A Prediction states the model id, the
probability that the contract's next settlement is above its current one, the data provenance (origin class and source ids of the records used, and the data
label shown to the reader), and an eligibility status:

ELIGIBLE      a probability exists and the model's standing allows it to drive a simulated trade on this data
DISPLAY_ONLY  a probability exists but must not drive a trade (the model has no demonstrated edge, or the data origin is not one the model may use)
UNAVAILABLE   no probability: parameters missing, an input missing or doubtful, or an unsupported contract

Eligibility is about this program's permission to act, not about whether the prediction is right. Nothing here says a prediction is accurate or profitable.
"""
from dataclasses import dataclass
from datetime import datetime

from . import features
from .models import DEMO_STANDING, PARAMS_OK

ELIGIBLE, DISPLAY_ONLY, UNAVAILABLE = "ELIGIBLE", "DISPLAY_ONLY", "UNAVAILABLE"
DATA_LABEL = {"SYNTHETIC_FIXTURE": "REPLAY OF INVENTED FIXTURE DATA (synthetic; not market data)"}


@dataclass(frozen=True)
class Prediction:
    as_of: datetime
    model_id: str
    contract: str
    probability: "float | None"
    status: str
    reasons: tuple                 # (code, text) pairs; empty only when ELIGIBLE
    feature: str
    feature_value: "float | None"
    data_observed_through: "datetime | None"
    provenance: dict               # origin_classes, source_ids, label, inputs
    standing: str

    def to_dict(self):
        return {"as_of": self.as_of.isoformat(), "model_id": self.model_id, "contract": self.contract,
                "probability": None if self.probability is None else format(self.probability, ".6f"), "status": self.status,
                "reasons": [list(r) for r in self.reasons], "feature": self.feature,
                "feature_value": None if self.feature_value is None else format(self.feature_value, ".10g"),
                "data_observed_through": None if self.data_observed_through is None else self.data_observed_through.isoformat(),
                "provenance": self.provenance, "standing": self.standing}


def _option_type(contract):
    parts = contract.split("|")
    return parts[-1] if len(parts) == 4 and parts[-1] in ("CE", "PE") else None


def predict(model, view, contract):
    card, now = model.card, view.now
    option_type = _option_type(contract)
    empty = {"origin_classes": [], "source_ids": [], "label": "no input data used", "inputs": []}

    def result(probability, status, reasons, feat=None, prov=None, observed=None):
        return Prediction(now, card.model_id, contract, probability, status, tuple(reasons), card.feature or "none",
                          None if feat is None else feat.value, observed, prov or empty, card.standing)

    if option_type is None:
        return result(None, UNAVAILABLE, [("UNSUPPORTED_CONTRACT", "the contract key is not NIFTY|expiry|strike|CE or PE")])
    if card.params_status != PARAMS_OK:
        return result(None, UNAVAILABLE, [(card.params_status, card.params_note or "the model has no usable parameters")])
    feat, prov, observed = None, None, None
    if card.feature is not None:
        feat = features.compute(card.feature, view, contract)
        if feat.value is None:
            return result(None, UNAVAILABLE, [(feat.code, feat.detail)], feat)
        origins = sorted({o for o, _ in feat.sources})
        prov = {"origin_classes": origins, "source_ids": sorted({s for _, s in feat.sources}), "inputs": [list(i) for i in feat.inputs],
                "label": "; ".join(DATA_LABEL.get(o, f"UNRECOGNISED ORIGIN {o}") for o in origins)}
        observed = feat.observed_through
        if observed > now:                                     # cannot happen through a PointInTimeView; checked again because it is the leakage rule
            return result(None, UNAVAILABLE, [("INPUT_FROM_THE_FUTURE", "an input was available after the prediction time")], feat)
    probability = model.probability(option_type, None if feat is None else feat.value)
    if probability is None:
        return result(None, UNAVAILABLE, [("NO_PROBABILITY", "the model has no probability for this option type")], feat, prov, observed)
    reasons = []
    if card.standing != DEMO_STANDING:
        reasons.append(("NO_DEMONSTRATED_EDGE", "the model has no demonstrated edge over the prior (see its evidence); it may be shown but not traded"))
    if feat is not None and "*" not in card.allowed_origins and not set(prov["origin_classes"]) <= set(card.allowed_origins):
        reasons.append(("ORIGIN_NOT_ALLOWED", f"the model may only be used on {', '.join(card.allowed_origins)} data"))
    return result(probability, DISPLAY_ONLY if reasons else ELIGIBLE, reasons, feat, prov, observed)
