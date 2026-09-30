from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXCEL_MAX_COLUMN = 16_384
EXCEL_MAX_ROW = 1_048_576
RANGE_PATTERN = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*):([A-Za-z]{1,3})([1-9][0-9]*)$")


def _column_number(letters: str) -> int:
    value = 0
    for character in letters.upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PageSelector(StrictInput):
    kind: Literal["page"]
    index: int = Field(ge=1)


class SlideSelector(StrictInput):
    kind: Literal["slide"]
    index: int = Field(ge=1)


class SheetSelector(StrictInput):
    kind: Literal["sheet"]
    name: str = Field(min_length=1, max_length=255)


class RangeSelector(StrictInput):
    kind: Literal["range"]
    sheet: str = Field(min_length=1, max_length=255)
    a1: str = Field(pattern=r"^[A-Za-z]{1,3}[1-9][0-9]*:[A-Za-z]{1,3}[1-9][0-9]*$")

    @model_validator(mode="after")
    def validate_excel_bounds(self) -> "RangeSelector":
        matched = RANGE_PATTERN.fullmatch(self.a1)
        if matched is None:
            return self
        start_column, start_row, end_column, end_row = matched.groups()
        start_column_number = _column_number(start_column)
        end_column_number = _column_number(end_column)
        start_row_number = int(start_row)
        end_row_number = int(end_row)
        if max(start_column_number, end_column_number) > EXCEL_MAX_COLUMN:
            raise ValueError("range columns exceed the XLSX worksheet boundary")
        if max(start_row_number, end_row_number) > EXCEL_MAX_ROW:
            raise ValueError("range rows exceed the XLSX worksheet boundary")
        if start_column_number > end_column_number or start_row_number > end_row_number:
            raise ValueError("range start must not follow range end")
        return self


class RegionSelector(StrictInput):
    kind: Literal["region"]
    unit_index: int = Field(default=1, ge=1)
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)
    coordinate_space: Literal["points", "pixels"]

    @model_validator(mode="after")
    def validate_finite_bounds(self) -> "RegionSelector":
        if not math.isfinite(self.x + self.width) or not math.isfinite(self.y + self.height):
            raise ValueError("region boundary must be finite")
        return self


Selector = Annotated[
    PageSelector | SlideSelector | SheetSelector | RangeSelector | RegionSelector,
    Field(discriminator="kind"),
]


def selector_dict(selector: Selector | None) -> dict[str, object] | None:
    return selector.model_dump(mode="json") if selector is not None else None
