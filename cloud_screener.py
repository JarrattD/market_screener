import asyncio
from pathlib import Path
from screenerV3.legacy_quality_audit import write_quality_audit
from screenerV3.completion import write_verified, resume_verified, publish_manifest, london_batch_date

service_account = './screener/service_account.json'
v1_path = './data/cleaned_tickers.json'
v2_path = './data/non_banking_tickers.json'

async def main(sheet_factory=None, screener_factories=None, batch_date=None,
               manifest_path='research_completion.json', pause=asyncio.sleep) -> None:
    if sheet_factory is None or screener_factories is None:
        from screenerV3.sheet import Sheet
        from screenerV3.payback_screener import PaybackScreener
        from screenerV3.multi_metric_screener import MultiMetricScreener
        sheet_factory = lambda name: Sheet(sheet_path=service_account, file_name=name)
        screener_factories = {
            'alpha': lambda: PaybackScreener(v1_path, sheet_path=service_account),
            'beta': lambda: MultiMetricScreener(v2_path, sheet_path=service_account),
        }
    Path(manifest_path).unlink(missing_ok=True)
    batch_date = batch_date or london_batch_date()
    receipts = []
    sheets = []
    for kind, name in [('alpha', 'Screener'), ('beta', 'V2 Screener')]:
        sheet = sheet_factory(name)
        sheets.append((kind, sheet))
        receipt = resume_verified(sheet, kind, batch_date)
        if receipt is None:
            # Crucially, constructor/Handler.process_tickers runs only for unfinished output.
            screener = screener_factories[kind]()
            if screener.sheet_client.file.id != sheet.file.id:
                raise ValueError('Screener destination changed')
            await screener.run_async(debug=False)
            handler = getattr(screener, "handler", None)
            if handler is not None and hasattr(handler, "quality_audit"):
                write_quality_audit(sheet, kind, handler.quality_audit, batch_date)
            receipt = write_verified(sheet, kind, screener.results, batch_date)
        receipts.append(receipt)
        if kind == 'alpha':
            await pause(60)
    # Recheck both frozen private checkpoints immediately before the public success marker.
    receipts = [resume_verified(sheet, kind, batch_date) for kind, sheet in sheets]
    publish_manifest(receipts, batch_date, manifest_path)

if __name__ == '__main__':
    asyncio.run(main())
