from datetime import datetime
from dotenv import load_dotenv
import pandas as pd
from Sheet import Sheet
from threading import Thread
import yfinance as yf
import os
import requests
import json

load_dotenv()
pd.set_option('display.float_format', lambda x: '%.3f' % x)

class Screener:
    def __init__(self, path: str= None) -> None:
        self.tickers = self.__process_tickers(path)
        self.key = os.environ['CLIENT_FMP_KEY']
        self.results = dict()
        self.sheet_client = Sheet()
        self.previous = self.sheet_client.get_previously_seen_tickers()
    
    def __read_json_file(self, file_path) -> dict[str:list]:
        with open(file_path, 'r') as file:
            data = json.load(file)
        return data
    
    def __process_tickers(self, path: str= None, tickers: dict[str:list] = None) -> dict[str:list]:
        t = self.__read_json_file(path)
        clean = {
                    'Japan': '.T',
                    'Canada': '.TO',
                    'Austria': '.VI',
                    'Belgium': '.BR',
                    'Estonia': '.TL',
                    'France': '.PA',
                    'Germany': '.DE',
                    'Greece': '.AT',
                    'Hungary': '.BD',
                    'Italy': '.MI',
                    'Latvia': '.RG',
                    'Lithuania': '.VS',
                    'Netherlands': '.AS',
                    'Poland': '.WS',
                    'Portugal': '.LS',
                    'Romania': '.RO',
                    'Finland': '.HE',
                    'Spain': '.MC',
                    'Sweden': '.ST',
                    'Switzerland': '.SW',
                    'United Kingdom': '.L',
                    'New Zealand': '.NZ',
                    'Czech Republic': '.PR',
                    'USA': ''}
        # for k, v in t.items():
        #     t[k] = [i.split(':')[1]+clean[k] for i in t[k]]
        
        return t
    
    def __has_dividends_or_buybacks(self, ticker: yf.Ticker) -> bool:
        has_dividends = ticker.dividends.sum() > 0
        if has_dividends:
            return True
        try:
            has_buybacks = ticker.quarterly_cashflow.loc['Repurchase Of Capital Stock'].sum() < 0
            return has_buybacks
        except:
            return False
        
    def __get_cashflow(self, ticker: str, span:int = 5) -> float:
        url = f'https://financialmodelingprep.com/api/v3/cash-flow-statement/{ticker}?period=annual&limit={span}&apikey={self.key}'
        response = requests.get(url)
        return response.json()

    def __get_profile(self, ticker: str) -> str:
        url = f'https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={self.key}'
        response = requests.get(url)
        return response.json()
    
    def __screen_dividends_and_buybacks(self, ret_dict:dict[str:dict], rs:list=[], debug:bool = False) -> list[str]:   
        ctn = 0
        m = []
        for k, v in ret_dict.items():
            try:
                ctn +=1
                t = yf.Ticker(k)
                has_dividends_or_buybacks = self.__has_dividends_or_buybacks(t)
                if has_dividends_or_buybacks:
                    v["Has Dividends or Buybacks"] = True
                else:
                    m.append(k)
            except:
                m.append(k)

            # TODO: Remove b4 shipping
            percentage_completed = (ctn + 1) / len(ret_dict) * 100
            if percentage_completed % 5 == 0 and debug:
                print(f"Processing: {percentage_completed:.0f}% complete")
        rs.append(ret_dict)
        return m
       
    def __screen_ncav(self, ret_dict:dict[str:dict], debug:bool = False) -> list[str]:
        ctn = 0
        m = []
        for k, v in ret_dict.items():
            try:
                ctn +=1
                t = yf.Ticker(k)
                qbs = t.quarterly_balance_sheet
                total_liabilities = qbs.loc['Total Liabilities Net Minority Interest'][0]
                current_assets = qbs.loc['Current Assets'][0]
                market_cap = t.info['marketCap']
                ncav = current_assets - total_liabilities
                is_market_cap_less_or_equal_to_ncav = market_cap <= ncav #self.__has_market_cap_less_than_ncav(t)
                if is_market_cap_less_or_equal_to_ncav:
                    v["Market Cap <= NCAV"] = True
                    v["Market Capitalization"] = int(market_cap)
                    v["NCAV Ratio"] = round(market_cap / ncav, 1)
                else:
                    m.append(k)
            except:
                m.append(k)

            # TODO: Remove b4 shipping
            percentage_completed = (ctn + 1) / len(ret_dict) * 100
            if percentage_completed % 5 == 0 and debug:
                print(f"Processing: {percentage_completed:.0f}% complete")
        
        return m

    def __screen_net_debt(self, ret_dict:dict[str:dict]) -> list[str]:
        m = []
        for k, v in ret_dict.items(): # check "Net Debt".
            try:
                ticker = yf.Ticker(k)
                net_debt = ticker.quarterly_balance_sheet.loc['Net Debt'][0]
                has_net_debt = net_debt > 0
                if has_net_debt:
                    v["Net Debt"] = net_debt
                else:
                    m.append(k)
            except:
                try:
                    net_debt = ticker.quarterly_balance_sheet.loc["Total Debt"][0]
                    has_net_debt = net_debt > 0 
                    if has_net_debt:
                        v["Net Debt"] = net_debt
                    else:
                        m.append(k)
                except:
                    m.append(k)
        
        return m

    def __screen_five_year_yield(self, ret_dict:dict[str:dict]) -> float:
        m = []
        print(f"{len(ret_dict)} stocks to be screened at `__screen_five_year_yield`")
        for k, v in ret_dict.items():
            try:
                profile = self.__get_profile(k)[0]
                mkt_cap = int(profile['mktCap'])
                v["Name"] = str(profile['companyName'])
                v["HQ Location"] = str(profile['country'])
                if v["HQ Location"] == "CN":
                    m.append(k)
                cashflow = self.__get_cashflow(k)
                five_year_fcf_average = sum([i['freeCashFlow'] for i in cashflow])/5
                v["5Y average"] = five_year_fcf_average
                val = round((five_year_fcf_average/mkt_cap)*100, 2)
                v['5Y average yield > 10%'] = val
                v["Cash & Equivalents"] = cashflow[0]["cashAtEndOfPeriod"]
                if val < 10:
                    m.append(k)
            except:
                m.append(k)
        
        return m 

    def __remove_previously_seen(self) -> None:
        drop = [i for i in self.results.keys() if i in self.previous]
        for i in drop:
            try:
                self.results.pop(i)
            except:
                continue
    
    def __split_dict(self, input_dict: dict, num_slices: int) -> list[dict]:
        dict_items = list(input_dict.items())
        slice_size = len(dict_items) // num_slices
        remainder = len(dict_items) % num_slices

        slices = []
        start = 0

        for i in range(num_slices):
            slice_end = start + slice_size + (1 if i < remainder else 0)
            slices.append(dict(dict_items[start:slice_end]))
            start = slice_end

        return slices
    
    def __calculate_packback_rating(self) -> None:
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
                v["Payback Rating"] = -1 # remove

    def __sort_results_dict(self)->None:
        self.results = dict(sorted(self.results.items(), key=lambda x: (x[1]["NCAV Ratio"], x[1]["Payback Rating"])))


    def __handle_threads(self, ret_dict: dict[str:list], start_time:datetime, debug:bool = False):
        steps = ['check "Has Dividends or Buybacks"', 'check "Market Cap <= NCAV"', 'check "Net Debt"', 'check "5Y average yield > 10%"','check "whitelist country"']
        removal_matrix = [[] for i in steps]

        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[0]}.")
        removal_matrix[0] = self.__screen_dividends_and_buybacks(ret_dict, debug=debug)
        print(f"Time to check dividends: {datetime.now() - start_time}")
        for i in removal_matrix[0]:
            ret_dict.pop(i)
    
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[1]}.")
        start_market_cap = datetime.now()
        removal_matrix[1] = self.__screen_ncav(ret_dict, debug=debug)
        print(f"Time to check Market Cap <= NCAV: {datetime.now() - start_market_cap}")
        for i in removal_matrix[1]:
            ret_dict.pop(i)
        
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[2]}.")
        start_net_debt = datetime.now()
        removal_matrix[2] = self.__screen_net_debt(ret_dict)
        print(f"Time to check Net Debt: {datetime.now() - start_net_debt}")
        for i in removal_matrix[2]:
            ret_dict.pop(i)
        
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[3]}.")
        start_fcf_time = datetime.now()
        removal_matrix[3] = self.__screen_five_year_yield(ret_dict)
        print(f"Time to check '5Y average yield > 10%': {datetime.now() - start_fcf_time}")
        for i in removal_matrix[3]:
            try:
                ret_dict.pop(i)
            except:
                continue
        
        self.results.update(ret_dict)
    
    def run(self, debug: bool = False) -> None:
        start_time = datetime.now()
        ticker_arr = [item for sub in self.tickers.values() for item in sub]
        ret_dict = {i:{"Name":str, "HQ Location":str, "Has Dividends or Buybacks": bool, "Net Debt": float, "5Y average yield > 10%": bool, "Market Cap <= NCAV": bool} for i in ticker_arr}
        steps = ['check "Has Dividends or Buybacks"', 'check "Market Cap <= NCAV"', 'check "Net Debt"', 'check "5Y average yield > 10%"','check "whitelist country"']
        removal_matrix = [[] for i in steps]
        
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[0]}.")
        removal_matrix[0] = self.__screen_dividends_and_buybacks(ret_dict, debug=debug)
        print(f"Time to check dividends: {datetime.now() - start_time}")
        for i in removal_matrix[0]:
            ret_dict.pop(i)
    
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[1]}.")
        start_market_cap = datetime.now()
        removal_matrix[1] = self.__screen_ncav(ret_dict, debug=debug)
        print(f"Time to check Market Cap <= NCAV: {datetime.now() - start_market_cap}")
        for i in removal_matrix[1]:
            ret_dict.pop(i)
        
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[2]}.")
        start_net_debt = datetime.now()
        removal_matrix[2] = self.__screen_net_debt(ret_dict)
        print(f"Time to check Net Debt: {datetime.now() - start_net_debt}")
        for i in removal_matrix[2]:
            ret_dict.pop(i)
        
        if debug:
            print(f"{len(ret_dict)} Tickers to be screened for step {steps[3]}.")
        start_fcf_time = datetime.now()
        removal_matrix[3] = self.__screen_five_year_yield(ret_dict)
        print(f"Time to check '5Y average yield > 10%': {datetime.now() - start_fcf_time}")
        for i in removal_matrix[3]:
            ret_dict.pop(i)
        
        self.results = ret_dict

        print(f"Total run time {datetime.now() - start_time}")
 
    def run_fully_threaded(self, thread_sum: int = 2, debug: bool = False) -> None:
        threads = []
        start = datetime.now()
        ticker_arr = [item for sub in self.tickers.values() for item in sub]
        ret_dict = {i:{"Name":str, "HQ Location":str, "Has Dividends or Buybacks": bool, "Net Debt": float, "Cash & Equivalents": float, "5Y average yield > 10%": bool, "5Y average": float, "Market Cap <= NCAV": bool, "Market Capitalization": float, "NCAV Ratio": float, "Payback Rating": float} for i in ticker_arr}
        split = self.__split_dict(ret_dict, thread_sum)
        for i in range(len(split)):
            is_last = i == len(split)-1
            thread = Thread(target= self.__handle_threads, args=[split[i], datetime.now(), is_last])
            threads.append(thread)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        
        print(f"Calculating payback rating for {len(self.results)} Stocks.")
        self.__calculate_packback_rating()
        negative_payback = []
        for k, v in self.results.items():
            if v["Payback Rating"] == -1:
                negative_payback.append(k)
        
        for i in negative_payback:
            self.results.pop(i)
        
        print(f"{len(self.results)} stocks remaining.")
        print(f"Total run time {datetime.now() - start}")
     
    def create_xlsx(self, file_path:str) -> None:
        self.__sort_results_dict()
        if len(self.results) == 0:
            print(f'ERROR: results dictionary is empty. Execute `Screener.run()` to screen the stocks. If you are still seeing this after running `Screener.run()`, there are no new stocks from the previous execution.')
        else:
            df = pd.DataFrame.from_dict(self.results, orient='index')
            df.to_excel(file_path)
            print(f"File saved to {file_path}")
    
    def update_google_sheet(self, debug:bool = False) -> None:
        starting_size = len(self.results)
        self.__remove_previously_seen()
        self.__sort_results_dict()
        cleaned = len(self.results)
        if debug:
            print(f"{starting_size - cleaned} tickers removed (previously present in google sheet).")
        
        self.sheet_client.add_row_data(self.results)
        self.sheet_client.sort_values()
        
        if debug:
            print("Google Sheet updated.")


    