"""Shared contract every receipt provider implements.

The schema and instructions live here so swapping providers can never change
what the frontend receives.
"""

import logging

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A provider failed. `transient` marks it worth retrying or failing over."""

    def __init__(self, message: str, *, transient: bool = False, status: int = 502):
        super().__init__(message)
        self.message = message
        self.transient = transient
        self.status = status


class NotAReceipt(ProviderError):
    """The model looked and says this photo is not a bill.

    Its own explanation is the message. Never transient and never worth
    failing over: a second reader will look at the same menu photo and say
    the same thing, slower.
    """

    def __init__(self, message: str):
        super().__init__(message, transient=False, status=422)


def noop_report(**_kwargs) -> None:
    """Progress sink for callers that aren't watching (tests, the CLI)."""


# Categories matter to the split, not just the display. Service charge and tax
# have to be re-apportioned across people in proportion to what each person
# actually ate — splitting them evenly is what made the old bills unfair — so
# the model has to tell us which lines are charges rather than food.
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {
            "type": "boolean",
            "description": (
                "True only if this image is a bill, receipt or itemised order "
                "with charged lines on it."
            ),
        },
        "reject_reason": {
            "type": "string",
            "description": (
                "When is_receipt is false: one plain sentence saying what the "
                "photo appears to be instead, and what to photograph. Empty "
                "string when is_receipt is true."
            ),
        },
        "currency": {
            "type": "string",
            "description": "ISO 4217 code if determinable, else empty string.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {
                        "type": "integer",
                        "description": "Units on this line. 1 when not printed.",
                    },
                    "line_total": {
                        "type": "number",
                        "description": "Total for the whole line, not the unit price.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "item",
                            "service_charge",
                            "tax",
                            "discount",
                            "rounding",
                        ],
                    },
                },
                "required": ["name", "quantity", "line_total", "category"],
            },
        },
        "subtotal": {"type": "number"},
        "total": {
            "type": "number",
            "description": "Grand total as printed. 0 if not shown.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One plain sentence per thing you could not read with "
                "confidence, naming the line it was on. Empty when the whole "
                "receipt was legible."
            ),
        },
    },
    "required": [
        "is_receipt",
        "reject_reason",
        "currency",
        "items",
        "subtotal",
        "total",
        "warnings",
    ],
}

SYSTEM_INSTRUCTION = """\
You read restaurant receipts and return structured data.

First decide whether the image is a bill at all. Set `is_receipt` false for
anything without charged lines on it — a menu, a photo of food, a screenshot,
a person, a blank or unreadable frame — and put one plain sentence in
`reject_reason` saying what it looks like instead and what to photograph, for
example "That's the menu, not the bill — photograph the printed receipt." Do
not invent items for an image that is not a receipt. When it is a receipt,
set `is_receipt` true and leave `reject_reason` empty.

Rules for a receipt:

- Return every charged line, in the order printed.
- `line_total` is the amount printed for that line — the total for all units on
  it, never the unit price. If only a unit price is printed, multiply it out.
- `quantity` is the number of units on the line; use 1 when none is printed.
- Classify each line: food and drink are "item"; service charge is
  "service_charge"; GST/VAT/sales tax is "tax"; discounts and vouchers are
  "discount" (negative line_total); rounding adjustments are "rounding".
- Tax already inside the prices is not a line. When the receipt marks it
  inclusive — "GST 9% (inc)", "inclusive of GST", "prices include tax" —
  leave it out entirely: it is inside every item total already, and adding it
  charges the table for it twice. Return a tax line only when the receipt
  adds it on top of the items.
- Sub-items printed under a set or combo with no price of their own are not
  separate lines. Fold their names into the parent, e.g.
  "Cocktail Party for 2 (Pineapple Rum, Ume Dream)".
- A heading that prints the total for the lines beneath it is not an item.
  Section names ("Chargeable Items", "Rice Plate (Chicken)", "Drinks") and
  running subtotals are headings even when a price sits on the same row —
  returning one would charge the table twice for everything under it. Return
  only the individual priced lines.
- Indented lines under a dish — packaging, add-ons, extra portions — are
  often a BREAKDOWN of the price already printed against that dish, not
  charges on top of it. Decide with arithmetic, not appearance: if the dish
  lines alone add up to the printed total, the indented lines are already
  inside them and must not be returned at all. Only return an add-on as its
  own line when leaving it out makes the receipt fail to add up.
  Worked example. A receipt prints:
      4 Salted Egg Chicken Rice        37.20
          4 Small Packaging             2.00
          4 Add egg                     4.00
      1 Seafood Hor Fun Small           9.50
          1 Small Packaging             0.50
      1 Hokkien Mee Small               7.50
          1 Small Packaging             0.50
      TOTAL                            54.20
  37.20 + 9.50 + 7.50 = 54.20, which is the total, so the packaging and the
  eggs are already inside those three prices. Return three lines, not seven.
  Returning seven would charge the table 61.20 for a 54.20 dinner.
- Always write `name` in English, whatever language the receipt is in. This is
  read at the table by people splitting a bill, so it has to be scannable.
  - Translate non-English names. Never return the original script, and never
    return a mix of both: "Salted egg chicken rice", not
    "咸蛋鸡丁饭 Creamy Salted Egg Chicken Rice".
  - Many receipts print the same dish twice, once per language. That is one
    line, not two.
  - Keep it to a few plain words describing the dish. Expand abbreviations and
    kitchen shorthand into something a diner would recognise: "Hor Fun w/ egg"
    becomes "Flat rice noodles with egg", "Kopi O" becomes "Black coffee".
  - Keep a restaurant's own name for a dish if it has no plain equivalent, but
    still write it in Latin script.
  - Modifiers priced on their own line stay their own line, named in relation
    to what they modify: "Extra rice", "Less ice".
- If a price is smudged or unreadable, use 0 rather than guessing, and say so
  in `warnings` naming the line: "The price on the second noodle line is
  cut off." Also warn when part of the receipt is out of frame, folded,
  covered by a finger or hand, lost to glare, or too blurred to read — say
  which part, so the person knows whether to reshoot. Say nothing in `warnings` about a receipt you read in
  full — an empty list is the normal case.
- `subtotal` is the pre-charge total; `total` is the grand total as printed.

Before you answer, add up the `line_total`s you are about to return and
compare them with the `total` you read. They should match. If they do not,
you have made a mistake somewhere — most often counting an indented
breakdown line as a charge, counting a section heading as an item, adding
tax that was already included, or misreading a digit. Find it and fix it.
Only if you genuinely cannot make them agree, return your best reading and
say in `warnings` what the difference is and where you think it comes from.
"""

PROMPT = "Extract every line from this receipt."


def reconcile_prompt(items, summed: float, total: float) -> str:
    """Show the model its own answer and the arithmetic that contradicts it.

    A bill that does not add up is nearly always a reading mistake, not a
    strange receipt, and the mistake is usually visible the moment the sum is
    put next to the printed total. Telling the person "these don't add up" is
    the last resort, not the first answer.
    """
    lines = "\n".join(
        "  %s x%s = %.2f" % (i.get("name"), i.get("quantity"), float(i.get("line_total") or 0))
        for i in items
    )
    return (
        "You read this receipt and returned these lines:\n\n"
        + lines
        + "\n\nThey add up to %.2f, but you read the printed total as %.2f "
        "\u2014 a difference of %.2f. One of those readings is wrong.\n\n"
        "Look at the image again and find which. The usual causes, in order "
        "of likelihood: an indented add-on line counted as a charge when its "
        "price was already inside the dish above it; a section heading "
        "counted as an item; tax marked inclusive added on top; a line "
        "missed; a digit misread; a discount or voucher skipped; or the "
        "figure you took as the total being a subtotal.\n\n"
        "Return the corrected receipt." % (summed, total, abs(summed - total))
    )
