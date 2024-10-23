from dotenv import load_dotenv
import pandas as pd
from .sheet import Sheet
from .utilities import Handler
from time import sleep
import aiohttp
import os

load_dotenv()


class MultiMetricScreener:
    def __init__(self, ticker_path: str, sheet_path:str = "./service_account.json", sheet_name: str = "V2 Screener") -> None:
        self.sheet_client = Sheet(sheet_path= sheet_path, file_name=sheet_name)
        self.handler = Handler()
        self.tickers = self.handler.process_tickers(self.sheet_client, ticker_path)
        self.profile_fstr_arr = self.__format_request_str(1000)
        self.hist_fstr_arr = self.__format_request_str(300)
        self.key = os.environ['FMP_KEY']
        self.results = {}
        self.floats = None
           
    def __get_ticker_count(self) -> int:
        num = 0
        for k, v in self.tickers.items():
            num += len(v)
        return num
    
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
    
    def __find_float_from_ticker(self, ticker) -> int:
        """
        Finds the float (outstanding shares) for a given ticker.

        Parameters:
        - `ticker` (str): The stock ticker symbol.

        Returns:
        - `int`: The number of outstanding shares, or 0 if not found.
        """
        for v in self.floats:
            if v['symbol'] == ticker:
                return v['outstandingShares']
        
        return 0 # if ticker can't be found, return 0
    
    def __check_reqs(self, requests_sent:int) -> None:
        if requests_sent % 299 == 0:
            print("Sleeping for 55 seconds to avoid hitting API limit.")
            sleep(55)
    
    def __clean_results(self, d:dict):
        ret = {}
        rem = 0
        for k, v in d.items():
            try:
                if v["Net Debt"] > 0:
                    rem +=1
                    continue # screen out stocks with net debt
                elif v["isAdded"]:
                    ret[k] = v
            except:
                rem+=1
                continue 
        
        for k, v in ret.items():
            try:
                if v["EV/aFCF"] == "N/A":
                    v["EV/aFCF"] = 1
            except:
                continue
        
        print(f"{rem}/{len(d.keys())} stocks removed during cleaning.")
        return ret
    
    def __sort_results(self) -> None:
        # sort first on NCAV (lowest -> highest)
        # second on upside (highest -> lowest)
        self.results = dict(sorted(self.results.items(), key=lambda x: (x[1]["P/TBV Ratio"], x[1]["FV Upside Metric"])))
       
    async def run_async(self, debug:bool=False) -> dict:
        stk_res = {}
        blacklist = ["CN", "HK"]
        issues = []
        requests_sent = 2
        starting_stocks = self.__get_ticker_count()
        self.floats = await self.handler.get_floats()
        requests_sent +=1
        print(f"Screening {starting_stocks} stocks...")
        async with aiohttp.ClientSession() as session:
            for string in self.profile_fstr_arr:
                res = await self.handler.get_profile(session, string)
                requests_sent += 1
                if res is None:
                    continue
                for profile in res:
                    try:
                        if int(profile['mktCap']) <= 0:
                            issues.append(profile['symbol'])
                            continue
                        if profile['country'] in blacklist:
                            issues.append(profile['symbol'])
                            continue
                    
                    
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
                        "Industry": profile["industry"]
                    }

            # get all balance sheet
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase I complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None
            issues = []
            for k, v in stk_res.items():
                self.__check_reqs(requests_sent)
                bs = await self.handler.get_balance_sheet(session, k)
                requests_sent += 1
                try: 
                    current_assets = int(bs[0]["totalCurrentAssets"])
                    total_liabilities = int(bs[0]["totalLiabilities"])
                    ncav = current_assets - total_liabilities
                    ratio = round(v["Market Cap"] / ncav, 1)
                    net_debt = int(bs[0]["netDebt"])
                    v["Net Debt"] = net_debt
                    v["NCAV Ratio"] = 1
                    if ratio > 0 and ratio < 2.5:
                        v["isAdded"] = True
                        v["NCAV Ratio"] = ratio

                except:
                    issues.append(k)
                    continue
            
            for i in issues:
                stk_res.pop(i)
            
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase II complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None
            issues = []
            for k, v in stk_res.items():
                self.__check_reqs(requests_sent)
                km = await self.handler.get_key_metrics(session, k)
                requests_sent += 1
                self.__check_reqs(requests_sent)
                cf = await self.handler.get_cashflow(session, k)
                requests_sent +=1
                try:
                    free_float = self.__find_float_from_ticker(k)
                    y_0_ttm = km[0]['freeCashFlowPerShareTTM'] * free_float
                    rest = [i['freeCashFlow'] for i in cf]
                    total = y_0_ttm + sum(rest)
                    five_year_fcf_average = total / 5 
                    pfcfRatio = v["Market Cap"]/five_year_fcf_average
                    v['5Y average'] = five_year_fcf_average
                    v["Cash & Equivalents"]= cf[0]["cashAtEndOfPeriod"]
                    
                    v["P/aFCF Ratio"] = round(pfcfRatio, 1)
                    if pfcfRatio > 0 and pfcfRatio < 10:
                        v["isAdded"] = True
                    
                    negCashflow = 0
                    for i in rest:
                        if i < 0:
                            negCashflow += 1
                    if negCashflow > 2:
                        issues.append(k)
                        continue
                    ev = km[0]["enterpriseValueTTM"]
                    v["EV"] = round(ev)
                    evFCF = ev/five_year_fcf_average
                    v["EV/aFCF"] = 100
                    if evFCF > 1 and evFCF < 5:
                        v["isAdded"] = True
                        v["EV/aFCF"] = round(evFCF, 1)
                    
                    pTBV = v["Market Cap"]/km[0]['tangibleAssetValueTTM']
                    v["P/TBV Ratio"] = round(pTBV)
                    
                    if pTBV > 0 and pTBV < 1:
                        v["isAdded"] = True
                    
                except Exception as e:
                    issues.append(k)
                    continue
        
            for i in issues:
                stk_res.pop(i)
            
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase III complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None
            issues = []
            for k, v in stk_res.items():
                self.__check_reqs(requests_sent)
                hist = await self.handler.get_historical(session, k)
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
            
            starting_stocks = starting_stocks-len(issues)
            print(f"Phase IV complete.\n{len(issues)} stocks removed.\n{starting_stocks} remaining.") if debug else None
            for i in issues:
                stk_res.pop(i)
            
            self.results = self.__clean_results(stk_res)
            self.__sort_results()
            return stk_res
        
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
    
    def update_google_sheet(self, debug:bool=False) -> None:
        self.sheet_client.create_beta_module_tab()
        self.sheet_client.add_beta_row_data(self.results)
        if debug:
            print("Google Sheet updated.")