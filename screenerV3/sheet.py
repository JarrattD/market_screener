from datetime import datetime
import gspread
from time import sleep
import re
from screenerV3.tab_order import history_worksheets

class Sheet:
    def __init__(self, sheet_path:str = "./service_account.json", file_name:str = 'Screener') -> None:
        self.service_account = gspread.service_account(filename = sheet_path)
        self.file = self.service_account.open(file_name)
        self.today = datetime.now()
        self._was_sheet_added_today = False
        self.month_dict = {
                            1: "Jan",
                            2: "Feb",
                            3: "Mar",
                            4: "Apr",
                            5: "May",
                            6: "Jun",
                            7: "Jul",
                            8: "Aug",
                            9: "Sep",
                            10: "Oct",
                            11: "Nov",
                            12: "Dec"}

    def __get_worksheet_names(self) -> list[gspread.Worksheet]:
        try:
            return self.file.worksheets()
        except:
            return []
    
    def __extract_date_from_string(self, input_str):
        pattern = r"'([^']+)'"
        match = re.search(pattern, input_str)
        if match:
            date_str = match.group(1)
            return date_str
        return None
    
    def __add_alpha_header(self) -> None:
        sheet = self.__get_worksheet_names()[-1]
        sheet.append_row(values= ["Ticker", "Company Name",  "FV Upside", "5Y Price Metric", " ", "NCAV Ratio","EV/aFCF", "Payback Rating", "Average Yield", "HQ Country"],table_range='A1:J1')

    def __add_beta_header(self) -> None:
        sheet = self.__get_worksheet_names()[-1]
        sheet.append_row(values= ["Ticker", "Company Name", "NCAV Ratio",  "EV/aFCF", "P/TBV Ratio", "HQ Location", " ", "FV Upside", "5Y Price Metric"],table_range='A1:I1')
    
    def add_alpha_row_data(self, data: dict):
        sheet = self.__get_worksheet_names()[-1]
        itr = 2
        for k, v in data.items():
            payload = [k, str(v['Name']), f"{v['FV Upside Metric']}%", f"{v['5Y Price Metric']}%", " ", v["NCAV Ratio"], v["EV/aFCF"], v["Payback Rating"], v["5Y average"], str(v['HQ Location'])]
            sheet.append_row(values= payload, table_range=f'A{itr}:J{itr}')
            itr+= 1
            sleep(2)
    
    def add_beta_row_data(self, data: dict):
        sheet = self.__get_worksheet_names()[-1]
        itr = 2
        for k, v in data.items():
            payload = [k, str(v['Name']), v["NCAV Ratio"], v["EV/aFCF"], v["P/TBV Ratio"], str(v['HQ Location']), " ", f"{v['FV Upside Metric']}%", f"{v['5Y Price Metric']}%"]
            sheet.append_row(values= payload, table_range=f'A{itr}:I{itr}')
            itr+= 1
            sleep(2)
    
    def get_all_worksheets(self) -> list[gspread.Worksheet]:
        try:
            return self.__get_worksheet_names()
        except:
            return []
    
    def create_alpha_module_tab(self):
        try:
            name = f"{self.today.day}-{self.month_dict[self.today.month]}-{self.today.year}"
            self.file.add_worksheet(title = name, rows = 0, cols = 0)
            self.__add_alpha_header()
            print(f"Sheet {name} added.")
            self._was_sheet_added_today = True
        except:
            print("Unable to add new tab. Tab already exists.")
    
    def create_beta_module_tab(self):
        try:
            name = f"{self.today.day}-{self.month_dict[self.today.month]}-{self.today.year}"
            self.file.add_worksheet(title = name, rows = 0, cols = 0)
            self.__add_beta_header()
            print(f"Sheet {name} added.")
            self._was_sheet_added_today = True
        except:
            print("Unable to add new tab. Tab already exists.")
    
    def get_previously_seen_tickers(self)-> list[str]:
        try:
            today = f"{self.today.day}-{self.month_dict[self.today.month]}-{self.today.year}"
            if today == self.__extract_date_from_string(str(self.__get_worksheet_names()[-1])):
                most_recent_sheet = self.__extract_date_from_string(str(self.__get_worksheet_names()[-2]))
            else:
                most_recent_sheet = self.__extract_date_from_string(str(self.__get_worksheet_names()[-1]))
            return [item for sublist in self.file.values_get(f"{most_recent_sheet}!A2:A100")['values'] for item in sublist]
        except:
            return []
    
    def get_all_previously_seen_tickers(self) -> list[str]:
        '''
        Main and V2: tickers still within their 12-calendar-month appearance cooldown.
        Other workbooks: retain the original first-52-tab history window.
        '''
        seen = []
        sheets = []
        sheets = history_worksheets(self.file, self.get_all_worksheets())
        try:
            for sheet in sheets:
                seen.append(sheet.get_values("A2:A1000"))
        except:
            print("Google Sheets API limit exceeded. Waiting 30 seconds before trying again.")
            sleep(31)
            self.get_all_previously_seen_tickers()
        
        return [i[0] for sub in seen for i in sub if i] 


