import asyncio
from screenerV3.payback_screener import PaybackScreener
from screenerV3.multi_metric_screener import MultiMetricScreener
from time import sleep
from pathlib import Path
from screenerV3.completion import write_verified, publish_manifest, london_batch_date

service_account = './screener/service_account.json'
v1_path = './data/cleaned_tickers.json'
v2_path = './data/non_banking_tickers.json'
test_path = './data/test_data.json'

async def main() -> None:
  Path('research_completion.json').unlink(missing_ok=True)
  batch_date = london_batch_date()
  receipts = []
  a = PaybackScreener(v1_path, sheet_path= service_account)
  await a.run_async(debug= False)
  receipts.append(write_verified(a.sheet_client, 'alpha', a.results, batch_date))
  print("Sleeping for 1 minute.")
  sleep(60)
  b = MultiMetricScreener(v2_path, sheet_path= service_account)
  await b.run_async(debug= False)
  receipts.append(write_verified(b.sheet_client, 'beta', b.results, batch_date))
  publish_manifest(receipts, batch_date)


if __name__ == "__main__":
    asyncio.run(main())

