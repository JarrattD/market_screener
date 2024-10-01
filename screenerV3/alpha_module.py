from dotenv import load_dotenv
from screener.Sheet import Sheet
import pandas as pd
from time import sleep
import aiohttp
import os
import json

load_dotenv()


class AlphaModule:
    def __init__(self, ticker_path: str, sheet_path:str = "./service_account.json", sheet_name: str = "Screener") -> None:
        self.sheet_client = Sheet(sheet_path= sheet_path, file_name=sheet_name)
        self.tickers = self.__process_tickers(ticker_path)
        self.profile_fstr_arr = self.__format_request_str(1000)
        self.hist_fstr_arr = self.__format_request_str(300)
        self.key = os.environ['FMP_KEY']
        self.results = {}
    
    def __read_json_file(self, file_path) -> dict[str:list]:
                """
                Reads a JSON file and returns its content as a dictionary.

                Parameters:
                - `file_path` (str): The path to the JSON file.

                Returns:
                - `dict`: A dictionary containing the content of the JSON file.
                """
                with open(file_path, 'r') as file:
                    data = json.load(file)
                return data

    def __process_tickers(self, path: str = None, tickers: dict[str:list] = None) -> dict[str:list]:
        """
        Processes tickers from a JSON file.

        Parameters:
        - `path` (str): The path to the JSON file containing stock tickers. Defaults to None.
        - `tickers` (dict): A dictionary of tickers. Defaults to None.

        Returns:
        - `dict`: A dictionary containing processed tickers.
        """
        t = self.__read_json_file(path)
        ret = {}
        previously_seen = self.sheet_client.get_all_previously_seen_tickers()
        removed = 0
        for k, v in t.items():
            init = len(v)
            ret[k]=[i for i in v if i not in previously_seen]
            removed += init-len(ret[k])
        
        print(f"{removed} tickers removed for being screened within the passed year.")
        return ret
    
    def __get_ticker_count(self) -> int:
        num = 0
        for k, v in self.tickers.items():
            num += len(v)
        return num


    async def __get_profile(self, session: aiohttp.ClientSession, ticker: str) -> str:
        async with session.get(f'https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={self.key}') as response:
            try:
                return await response.json()
            except Exception as e:
                pass
    
    async def __get_historical(self, session: aiohttp.ClientSession, ticker: str) -> str:
        async with session.get(f'https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}?apikey={self.key}') as response:
            try:
                return await response.json()# if response.status == 200 else print("Hit API limit. Waiting 55 seconds.") and sleep(55)
            except Exception as e:
                pass
    
    async def __get_balance_sheet(self, session: aiohttp.ClientSession, ticker: str) -> str:
        async with session.get(f'https://financialmodelingprep.com/api/v3/balance-sheet-statement/{ticker}?period=quarter&limit=5&apikey={self.key}') as response:
            try:
                return await response.json()# if response.status == 200 else print("Hit API limit. Waiting 55 seconds.") and sleep(55)
            except Exception as e:
                pass
    
    async def __get_cashflow(self, session: aiohttp.ClientSession, ticker: str) -> str:
        async with session.get(f'https://financialmodelingprep.com/api/v3/cash-flow-statement/{ticker}?period=annual&limit=5&apikey={self.key}') as response:
            try:
                return await response.json()# if response.status == 200 else print("Hit API limit. Waiting 55 seconds.") and sleep(55)
            except Exception as e:
                pass

    def __calculate_packback_rating(self, debug: bool = False) -> None:
        """
        Calculates the payback rating for the screening results.

        Returns:
        - `None`
        """
        negative_payback_rating = []
        for k, v in self.results.items():
            for k, v in self.results.items():
                cash_equivalents = v.get("Cash & Equivalents", 0)
                earnings_average = v.get("5Y average", 0)
                market_cap = v.get("Market Capitalization", 0)
                if cash_equivalents > market_cap:
                    v["Payback Rating"] = 0.5
                elif market_cap <= (cash_equivalents + earnings_average):
                    v["Payback Rating"] = 1
                elif market_cap <= (cash_equivalents + (earnings_average * 2)):
                    v["Payback Rating"] = 2
                elif market_cap <= (cash_equivalents + (earnings_average * 3)):
                    v["Payback Rating"] = 3
                else:
                    negative_payback_rating.append(k)

            for i in negative_payback_rating:
                self.results.pop(i)
        print(f"{len(negative_payback_rating)} tickers removed for negative payback rating.") if debug else None

    def __format_request_str(self, limit:int=300) -> str:
        """
        Formats the tickers into a single request string, limited by the specified number.

        Parameters:
        - `limit` (int): The maximum number of tickers to include in the request string.

        Returns:
        - `str`: A formatted string of tickers, separated by commas.
        """
        all_tickers = []
        for country, tickers in self.tickers.items():
            for ticker in tickers:
                all_tickers.append(ticker)

        request_strings = [
            ",".join(all_tickers[i:i + limit]) 
            for i in range(0, len(all_tickers), limit)
        ]
        
        return request_strings
    
    def __check_reqs(self, requests_sent:int) -> None:
        if requests_sent % 299 == 0:
            print("Sleeping for 55 seconds to avoid hitting API limit.")
            sleep(55)
    
    def __sort_results(self) -> None:
        # sort first on NCAV (lowest -> highest)
        # second on upside (highest -> lowest)
        self.results = dict(sorted(self.results.items(), key=lambda x: (x[1]["NCAV Ratio"], x[1]["FV Upside Metric"])))
    

    async def run_async(self, debug:bool=False) -> dict:
        stk_res = {}
        blacklist = ["CN", "HK"]
        issues = []
        requests_sent = 0
        starting_stocks = self.__get_ticker_count()
        print(f"Screening {starting_stocks} stocks...")
        async with aiohttp.ClientSession() as session: 
            for string in self.profile_fstr_arr:
                res = await self.__get_profile(session, string)
                requests_sent += 1
                if res is None:
                    continue
                for profile in res:
                    if int(profile['mktCap']) <= 0:
                        issues.append(profile['symbol'])
                        continue
                    if profile['country'] in blacklist:
                        issues.append(profile['symbol'])
                        continue
                    
                    try:
                        if profile['industry'][:5] == "Banks" or profile['industry'][:9] == "Insurance" or profile["industry"][:9] == "Financial" or profile['industry'][:10] == "Investment" or profile['industry'] == "Asset Management":
                            issues.append(profile['symbol'])
                            continue
    
                        div = float(profile.get("lastDiv", 0))
                    except (IndexError, ValueError, TypeError):
                        issues.append(profile['symbol'])
                        continue

                    stk_res[profile['symbol']] = {
                        "Name": profile["companyName"],
                        "Market Cap": profile['mktCap'],
                        "HQ Location": profile["country"],
                        "Exchange Location": profile["exchange"],
                        "Industry": profile["industry"],
                        "Has Dividends or Buybacks":div 
                    }

            # get all cashflow
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase I complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None
            issues = []
            for k, v in stk_res.items():
                self.__check_reqs(requests_sent)
                cf = await self.__get_cashflow(session, k)
                requests_sent += 1
                try:
                    if v['Has Dividends or Buybacks'] < 1:
                        buyback = sum([i["commonStockRepurchased"]
                                      for i in cf])
                        if buyback < 0:
                            v['Has Dividends or Buybacks'] = 'buyback'
                    five_year_fcf_average = sum(
                        [i['freeCashFlow'] for i in cf])/5
                    average_yield = round(
                        (five_year_fcf_average/v['Market Cap'])*100, 2)
                    if average_yield < 10:
                        issues.append(k)
                        continue
                    v['5Y average yield > 10%'] = average_yield
                    v['5Y average'] = five_year_fcf_average
                    v["Cash & Equivalents"]= cf[0]["cashAtEndOfPeriod"]
                    if v['Has Dividends or Buybacks'] == 0:
                        issues.append(k)
                        continue

                except Exception as ex:
                    issues.append(k)
                    print(f"removing for: {ex}") if debug else None
                    continue
            for i in issues:
                stk_res.pop(i)
            
            
            # get all balencesheets
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase II complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None
            issues = []
            for k, v in stk_res.items():
                self.__check_reqs(requests_sent)
                bs = await self.__get_balance_sheet(session, k)
                requests_sent += 1
                try:
                    net_debt = int(bs[0]["netDebt"])
                    if net_debt > 0:
                        issues.append(k)
                        continue
                    current_assets = int(bs[0]["totalCurrentAssets"])
                    total_liabilities = int(bs[0]["totalLiabilities"])
                    ncav = current_assets - total_liabilities
                    if ncav < 0:
                        issues.append(k)
                        continue
                    v['NCAV'] = ncav
                    v['NCAV Ratio'] = round(v['Market Cap']/ncav, 1)
                except:
                    issues.append(k)
                    continue
            
            for i in issues:
                stk_res.pop(i)
            
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase III complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None

            issues = []
            for k, v in stk_res.items():
                self.__check_reqs(requests_sent)
                hist = await self.__get_historical(session, k)
                requests_sent += 1 
                try:
                    five_year_max = round(max([i['close'] for i in hist['historical']]), 2)
                    five_year_price_metric = ((five_year_max - hist['historical'][0]['close'])/hist['historical'][0]['close']) * 100
                    v['5Y Price Metric'] = round(five_year_price_metric)
                    v['Current Price'] = round(hist['historical'][0]['close'], 2)
                    v['5Y Max'] = five_year_max
                except:
                    issues.append(k)
                    continue
            
            for i in issues:
                stk_res.pop(i)
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase IV complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None

            issues = []
            for k, v in stk_res.items():
                try:
                    fv_upside = (v['5Y average'] * 7) + v["Cash & Equivalents"]
                    upside_percentage = ((fv_upside-v['Market Cap'])/v['Market Cap']) * 100
                    v['FV Upside Metric'] = round(upside_percentage)
                except:
                    issues.append(k)
            
            for i in issues:
                stk_res.pop(i)
            
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase V complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None

        print(f"{requests_sent} requests sent") if debug else None
        self.results = stk_res
        self.__calculate_packback_rating(debug)
        self.__sort_results()
        return self.results
    
    def update_google_sheet(self, debug:bool=False) -> None:
        self.sheet_client.create_new_tab()
        self.sheet_client.add_row_data(self.results)
        print("Google Sheet updated.") if debug else None
    
    def create_xlsx(self, file_path:str) -> None:
        """
        Creates an Excel file with the screening results.

        Parameters:
        - `file_path` (str): The path to the Excel file.

        Returns:
        - `None`
        """
        if len(self.results) == 0:
            print(f'ERROR: results dictionary is empty. Execute `Screener.run()` to screen the stocks. If you are still seeing this after running `Screener.run()`, there are no new stocks from the previous execution.')
        else:
            df = pd.DataFrame.from_dict(self.results, orient='index')
            df.to_excel(file_path)
            print(f"File saved to {file_path}")