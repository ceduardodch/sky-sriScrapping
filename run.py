#!/usr/bin/env python3
"""
SRI Ecuador Portal Scraper - Fase 1
Uso:
    python run.py scrape                      # pipeline completo (fecha = ayer)
    python run.py scrape --date 2024-03-01    # fecha especifica
    python run.py scrape --headless           # sin ventana de browser
    python run.py parse-file downloads/sri_recibidos_20240301.txt
"""

from __future__ import annotations

# Forzar UTF-8 en Windows antes de cualquier print/Rich
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import asyncio
from datetime import date
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from sri_scraper.config import SRIConfig
from sri_scraper.exceptions import (
    LoginError,
    MaintenanceError,
    NavigationError,
    SRIScraperError,
)
from sri_scraper.parser import ClaveDeAcceso, extract_claves_from_file
from sri_scraper.pipeline import scrape_recibidos
from sri_scraper.soap_client import SRISOAPClient, save_xml

console = Console(force_terminal=True, highlight=False)


def _configure_logging(config: SRIConfig) -> None:
    """Configura structlog para escribir JSON a archivo y texto a consola."""
    import logging
    import structlog

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.logs_dir / "sri_scraper.log"

    logging.basicConfig(
        format="%(message)s",
        level=logging.WARNING,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.WriteLoggerFactory(
            file=log_file.open("a", encoding="utf-8")
        ),
    )


def _print_claves_table(claves: list[ClaveDeAcceso]) -> None:
    """Imprime las claves parseadas en una tabla Rich."""
    if not claves:
        console.print("[yellow]No se encontraron Claves de Acceso en el archivo.[/yellow]")
        return

    table = Table(title=f"Claves de Acceso SRI ({len(claves)} encontradas)")
    table.add_column("Tipo", style="cyan", no_wrap=True)
    table.add_column("Fecha", style="green")
    table.add_column("RUC Emisor", style="blue")
    table.add_column("Número", style="white")
    table.add_column("Ambiente", style="magenta")
    table.add_column("Válida", style="bold")
    table.add_column("Clave (primeros 15)", style="dim")

    for c in claves:
        valid_icon = "[green]OK[/green]" if c.is_valid else "[red]NO[/red]"
        table.add_row(
            c.tipo_comprobante_desc[:20],
            c.fecha_emision.strftime("%d/%m/%Y"),
            c.ruc_emisor,
            c.numero_completo,
            c.ambiente,
            valid_icon,
            c.raw[:15] + "...",
        )

    console.print(table)

    invalid_count = sum(1 for c in claves if not c.is_valid)
    if invalid_count:
        console.print(f"[yellow][!] {invalid_count} claves con checksum invalido (incluidas de todas formas)[/yellow]")


# ─── Comando: scrape ──────────────────────────────────────────────────────────

@click.command("scrape")
@click.option(
    "--date", "report_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Fecha del reporte (YYYY-MM-DD). Default: ayer.",
)
@click.option("--headless", is_flag=True, default=False, help="Ejecutar sin ventana de browser.")
def cmd_scrape(report_date: Optional[date], headless: bool) -> None:
    """Pipeline completo: login → descarga TXT → parse de Claves de Acceso."""

    # Cargar config desde .env
    try:
        config = SRIConfig()
    except Exception as e:
        console.print(f"[red]Error al cargar configuración:[/red] {e}")
        console.print("[yellow]Asegúrate de crear el archivo .env (ver .env.example)[/yellow]")
        sys.exit(1)

    if report_date:
        config = config.model_copy(update={"report_date": report_date.date()})
    if headless:
        config = config.model_copy(update={"headless": True})

    _configure_logging(config)

    console.print(f"\n[bold blue]SRI Ecuador Scraper — Fase 1[/bold blue]")
    console.print(f"RUC: {config.sri_ruc[:4]}***")
    console.print(f"Fecha objetivo: [bold]{config.effective_report_date}[/bold]")
    console.print(f"Modo: {'headless' if config.headless else 'con ventana (headed)'}\n")

    asyncio.run(_run_pipeline(config))


async def _run_pipeline(config: SRIConfig) -> None:
    """Ejecuta el pipeline completo de scraping."""
    try:
        console.print("[cyan]>> Paso 1/4:[/cyan] Ejecutando pipeline compartido del scraper...")
        result = await scrape_recibidos(config)

        if result.txt_path is None:
            console.print(
                f"[yellow]  [!] No hay comprobantes recibidos para el "
                f"{config.effective_report_date.strftime('%d/%m/%Y')}[/yellow]"
            )
            return

        console.print(f"[green]  [OK] TXT guardado: {result.txt_path}[/green]")

        console.print("[cyan]>> Paso 4/4:[/cyan] Parseando Claves de Acceso...")
        claves = result.claves
        console.print(f"[green]  [OK] {len(claves)} claves extraidas[/green]\n")

        _print_claves_table(claves)

        # Guardar claves en JSON para fases posteriores
        import json
        json_path = config.downloads_dir / f"claves_{config.effective_report_date.strftime('%Y%m%d')}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "clave": c.raw,
                        "tipo": c.tipo_comprobante_desc,
                        "fecha": str(c.fecha_emision),
                        "ruc_emisor": c.ruc_emisor,
                        "numero": c.numero_completo,
                        "ambiente": c.ambiente,
                        "valida": c.is_valid,
                    }
                    for c in claves
                ],
                f,
                ensure_ascii=False,
                indent=2,
            )
        console.print(f"\n[dim]Claves guardadas en JSON: {json_path}[/dim]")

    except MaintenanceError as e:
        console.print(f"[yellow][!] Portal SRI en mantenimiento:[/yellow] {e}")
        sys.exit(2)
    except LoginError as e:
        console.print(f"[red][ERROR] Login:[/red] {e}")
        sys.exit(1)
    except (NavigationError, SRIScraperError) as e:
        console.print(f"[red][ERROR][/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red][ERROR] Inesperado:[/red] {type(e).__name__}: {e}")
        raise


# ─── Comando: download-xml ────────────────────────────────────────────────────

@click.command("download-xml")
@click.argument("json_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--ambiente",
    type=click.Choice(["produccion", "pruebas"], case_sensitive=False),
    default="produccion",
    show_default=True,
    help="Endpoint SOAP del SRI.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directorio destino para los XML. Default: downloads/xml/<fecha>/",
)
@click.option("--timeout", default=30, show_default=True, help="Timeout SOAP en segundos.")
def cmd_download_xml(
    json_file: Path,
    ambiente: str,
    output_dir: Optional[Path],
    timeout: int,
) -> None:
    """
    Descarga los XML autorizados del SRI vía SOAP para cada clave en JSON_FILE.

    JSON_FILE debe ser el archivo claves_YYYYMMDD.json generado por el comando scrape.

    Ejemplo:
        python run.py download-xml downloads/claves_20260309.json
    """
    import json

    console.print(f"\n[bold blue]SRI Ecuador — Fase 2: Descarga XML via SOAP[/bold blue]")
    console.print(f"Fuente:   {json_file}")
    console.print(f"Ambiente: [bold]{ambiente}[/bold]\n")

    with open(json_file, encoding="utf-8") as f:
        claves_data = json.load(f)

    if not claves_data:
        console.print("[yellow]El archivo JSON no contiene claves de acceso.[/yellow]")
        return

    console.print(f"[cyan]Claves a procesar:[/cyan] {len(claves_data)}\n")

    client = SRISOAPClient(ambiente=ambiente, timeout=timeout)

    # Directorio destino por defecto: downloads/xml/<fecha del primer comprobante>
    if output_dir is None:
        fecha_ref = claves_data[0].get("fecha", "sin_fecha").replace("-", "")
        output_dir = Path("downloads") / "xml" / fecha_ref

    # Config para leer api_url / api_key / api_tenant_id del .env
    try:
        _cfg = SRIConfig()
        api_url = _cfg.api_url
        api_key = _cfg.api_key
        api_tenant_id = _cfg.api_tenant_id
    except Exception:
        api_url = api_key = api_tenant_id = None

    push_to_api = bool(api_url and api_key and api_tenant_id)
    if push_to_api:
        console.print(f"[dim]Push a API:[/dim] {api_url} (tenant {api_tenant_id})\n")

    results = []
    ok = err = 0

    async def _push_all(ok_results):
        from sri_scraper.api_client import push_xml_to_api
        for r in ok_results:
            await push_xml_to_api(r, tenant_id=api_tenant_id, api_url=api_url, admin_api_key=api_key)

    ok_results = []

    for entry in claves_data:
        clave = entry["clave"]
        tipo  = entry.get("tipo", "?")
        num   = entry.get("numero", "?")

        console.print(f"  [dim]{clave[:15]}...[/dim] [{tipo}] {num} → ", end="")

        result = client.autorizar_comprobante(clave)
        results.append(result)

        if result.autorizado and result.tiene_xml:
            if push_to_api:
                ok_results.append(result)
                console.print(f"[green]OK[/green] → (pendiente push API)")
            else:
                xml_path = save_xml(result, output_dir)
                console.print(f"[green]OK[/green] → {xml_path.name if xml_path else '?'}")
            ok += 1
        elif result.estado == "AUTORIZADO" and not result.tiene_xml:
            console.print(f"[yellow]AUTORIZADO (sin XML)[/yellow]")
            ok += 1
        else:
            msgs = "; ".join(m.get("mensaje", "") for m in result.mensajes) or result.error or ""
            console.print(f"[red]{result.estado}[/red] {msgs[:60]}")
            err += 1

    if push_to_api and ok_results:
        console.print(f"\n[cyan]Pusheando {len(ok_results)} XML a la API...[/cyan]")
        asyncio.run(_push_all(ok_results))
        console.print(f"[green]Push completado[/green]")

    console.print(f"\n[bold]Resumen:[/bold] {ok} OK / {err} con error")
    if ok > 0 and not push_to_api:
        console.print(f"[dim]XML guardados en: {output_dir}[/dim]")


# ─── Comando: parse-file ──────────────────────────────────────────────────────

@click.command("parse-file")
@click.argument("filepath", type=click.Path(exists=True, path_type=Path))
def cmd_parse_file(filepath: Path) -> None:
    """Parsea un archivo TXT del SRI ya descargado y muestra las Claves de Acceso."""
    console.print(f"\n[bold blue]Parseando:[/bold blue] {filepath}\n")
    claves = extract_claves_from_file(filepath)
    _print_claves_table(claves)


# ─── CLI principal ────────────────────────────────────────────────────────────

@click.group()
def cli():
    """SRI Ecuador Portal Scraper — login + descarga TXT + XML via SOAP."""
    pass


cli.add_command(cmd_scrape)
cli.add_command(cmd_download_xml)
cli.add_command(cmd_parse_file)


if __name__ == "__main__":
    cli()
