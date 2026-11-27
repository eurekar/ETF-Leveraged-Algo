# region imports
from starter_core import StarterSystem
from rules import build_rules, DEFAULT_RULES
# endregion
"""
Stage 3: combine several rules into one binary trade direction.

Each rule votes +1 / -1 (0 if it has no opinion). The combined vote is the
weighted average of the votes of the rules that are ready:

    vote = sum(w_i * v_i) / sum(w_i)        over ready rules, in [-1, +1]

Trade direction = sign(vote). Everything else (stop, sizing fixed at entry,
re-entry rule) is the Stage 2 Starter System, unchanged.

Weights are set per rule family and split evenly inside the family:

    mac 30%  (4 speeds -> 7.5% each)
    breakout 30%  (4 speeds -> 7.5% each)
    carry 40%

With these weights a tie cannot happen once all rules are ready. Carver's
handcrafted 25/25/50 (w_carry=0.5) is available as a parameter, but in this
binary, stop-based stage it freezes the system: trend can never outvote
carry, so direction = sign(carry), and after a stop the re-entry rule waits
for carry to change sign (see stage3_multi_rule/README.md). Any tie gives
signal 0: a flat ETF waits until a rule changes.

Forecast scalars and the forecast diversification multiplier do not appear
here on purpose: multiplying by a positive number never changes a sign, so
they only matter once position size follows forecast strength (Stage 5).
"""

DEFAULT_FAMILY_WEIGHTS = {"mac": 0.3, "breakout": 0.3, "carry": 0.4}


def rule_weights(rules, family_weights=None):
    """{name: weight}, weights summing to 1 over the rules given."""
    fw = dict(DEFAULT_FAMILY_WEIGHTS if family_weights is None else family_weights)
    counts = {}
    for fam, _ in rules.values():
        counts[fam] = counts.get(fam, 0) + 1
    total = sum(fw.get(f, 0.0) for f in counts)
    if total <= 0:
        raise ValueError("All selected rule families have zero weight")
    return {name: fw.get(fam, 0.0) / total / counts[fam] for name, (fam, _) in rules.items()}


class MultiRuleSystem(StarterSystem):
    """Starter System whose opening signal is the combined vote of several rules."""

    def __init__(self, rules_spec=DEFAULT_RULES, family_weights=None, ma_type="sma",
                 funding_spread=0.0, risk_window=25, stop_fraction=0.5, allow_short=True,
                 wait_for_flip=False):
        super().__init__(signal_type=ma_type, risk_window=risk_window,
                         stop_fraction=stop_fraction, allow_short=allow_short,
                         wait_for_flip=wait_for_flip)
        self.rules = build_rules(rules_spec, ma_type, funding_spread)
        self.weights = rule_weights(self.rules, family_weights)
        self.price_rules = [r for fam, r in self.rules.values() if fam != "carry"]
        self.carry_rule = self.rules["carry"][1] if "carry" in self.rules else None

    # ---------- data ----------
    def add_close(self, close, date=None, risk_free_rate=None):
        super().add_close(close)
        for r in self.price_rules:
            r.update(close)
        if self.carry_rule is not None and date is not None:
            self.carry_rule.update(date, risk_free_rate, self.instrument_risk())

    def add_distribution(self, date, amount, ref_price):
        if self.carry_rule is not None:
            self.carry_rule.add_distribution(date, amount, ref_price)

    # ---------- signal ----------
    @property
    def ready(self):
        """Risk estimate available and every price rule warmed up.
        Carry may still be warming up; it then sits out and the weights renormalise."""
        if self.n < self.risk_window + 1:
            return False
        if self.price_rules:
            return all(r.ready for r in self.price_rules)
        return self.carry_rule is not None and self.carry_rule.ready

    def votes(self):
        return {name: (r.vote() if r.ready else None) for name, (_, r) in self.rules.items()}

    def combined_vote(self):
        num = den = 0.0
        for name, (_, r) in self.rules.items():
            if r.ready:
                w = self.weights[name]
                num += w * r.vote()
                den += w
        return num / den if den > 0 else 0.0

    def signal(self):
        if not self.ready:
            return 0
        v = self.combined_vote()
        eps = 1e-12
        return 1 if v > eps else -1 if v < -eps else 0
