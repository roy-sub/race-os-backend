"""Shapes shared by more than one router.

Kept here rather than duplicated so that a caveat means the same thing on a
plan as it does on a dashboard, and so the generated OpenAPI has one
definition for it instead of three that can drift apart.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from raceos.api.errors import ErrorCode


class ResponseWarningOut(BaseModel):
    """A non-blocking caveat riding alongside a successful response.

    The distinction from an error is load-bearing and is why this exists as a
    field rather than as a status code: an error *replaces* the response, a
    warning *accompanies* it. A plan built on a six-month-old FTP is still a
    plan, and refusing to return it would serve the athlete worse than
    returning it with the caveat attached.

    Only the codes in :data:`~raceos.api.errors.WARNING_CODES` can appear here;
    :meth:`~raceos.api.errors.WarningCollector.add` refuses anything else.
    """

    model_config = ConfigDict(from_attributes=True)

    code: ErrorCode
    message: str
    #: The constraint key or input field the caveat is about, when it is about
    #: one. A screen uses it to put the warning beside the value it concerns.
    field: str | None = None
