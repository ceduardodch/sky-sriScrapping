"""
Endpoint de consulta de clientes desde base_maestra.
Requiere X-API-Key: MAESTRA_API_KEY
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_maestra_session
from ..schemas import ClienteFacturacionOut, ErrorResponse

router = APIRouter(prefix="/api/v1", tags=["clientes"])

_maestra_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_maestra(api_key: str = Security(_maestra_key_header)) -> None:
    if not settings.maestra_api_key:
        raise HTTPException(
            status_code=503,
            detail="Servicio de clientes no configurado (MAESTRA_API_KEY no definida)",
        )
    if not api_key or api_key != settings.maestra_api_key:
        raise HTTPException(status_code=403, detail="API key inválida o no autorizada")


@router.get(
    "/clientes/{identity}",
    response_model=ClienteFacturacionOut,
    summary="Consultar cliente por cédula o RUC",
    description=(
        "Devuelve los datos básicos de un cliente para facturación electrónica.\n\n"
        "El parámetro `identity` puede ser:\n"
        "- **Cédula** de 10 dígitos\n"
        "- **RUC** de 13 dígitos\n\n"
        "Requiere header `X-API-Key: <MAESTRA_API_KEY>`.\n\n"
        "Ejemplo:\n"
        "```bash\n"
        "curl http://127.0.0.1:8000/api/v1/clientes/1713209771001 \\\n"
        "  -H 'X-API-Key: <MAESTRA_API_KEY>'\n"
        "```"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Cliente no encontrado"},
        403: {"model": ErrorResponse, "description": "API key inválida"},
        503: {"model": ErrorResponse, "description": "Servicio no configurado"},
    },
    dependencies=[Depends(verify_maestra)],
)
async def get_cliente(
    identity: str,
    session: AsyncSession = Depends(get_maestra_session),
) -> ClienteFacturacionOut:
    # Busca por cédula exacta o por RUC dentro del array rucs
    result = await session.execute(
        text("""
            SELECT
                identity,
                full_name,
                names,
                lastnames,
                rucs,
                emails,
                phones,
                latest_address,
                latest_postcode,
                province_code,
                canton_code,
                nationality
            FROM master.clientes
            WHERE identity = :identity
               OR :identity = ANY(rucs)
            LIMIT 1
        """),
        {"identity": identity},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Cliente con identidad '{identity}' no encontrado",
        )

    return ClienteFacturacionOut(
        identity=row["identity"],
        full_name=row["full_name"],
        names=row["names"],
        lastnames=row["lastnames"],
        rucs=row["rucs"],
        emails=row["emails"],
        phones=row["phones"],
        latest_address=row["latest_address"],
        latest_postcode=row["latest_postcode"],
        province_code=row["province_code"],
        canton_code=row["canton_code"],
        nationality=row["nationality"],
    )
