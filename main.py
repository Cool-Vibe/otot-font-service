import base64
import hmac
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from engine import Params, build_family

app = FastAPI(title="OtOt Pro Font Service")


class BuildRequest(BaseModel):
    latinFamily: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9 ]{1,40}$")
    hebrewFamily: str = Field(min_length=1, max_length=40)
    light: dict
    bold: dict
    regularT: float = Field(ge=0, le=1)
    tight: float = Field(ge=0, le=1, default=0.5)
    italicAngle: float = Field(ge=-20, le=0, default=0)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/build")
def build(req: BuildRequest, authorization: str = Header(default="")):
    token = os.environ.get("FONT_SERVICE_TOKEN", "")
    if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "unauthorized")
    if len(req.light.get("glyphs", [])) != len(req.bold.get("glyphs", [])):
        raise HTTPException(400, "masters are not compatible")
    data, report = build_family(Params(
        latin_family=req.latinFamily, hebrew_family=req.hebrewFamily, light=req.light, bold=req.bold,
        regular_t=req.regularT, tight=req.tight, italic_angle=req.italicAngle,
    ))
    return {"zip": base64.b64encode(data).decode(), "report": report}
