"""
This module contains classes responsible for communicating with
Google Data API and common spreadsheets models.
"""
import csv
import gdata.gauth
import gspread 
import io
import json
import xlrd
    
from django.conf import settings
from django.core.files.storage import get_storage_class
from oauth2client.client import SignedJwtAssertionCredentials
from onadata.koboform.pyxform_utils import convert_csv_to_xls
from onadata.libs.utils.google import get_refreshed_token
from onadata.libs.utils.export_builder import ExportBuilder
from onadata.libs.utils.common_tags import INDEX, PARENT_INDEX, PARENT_TABLE_NAME

def update_row(worksheet, index, values):
    """"Adds a row to the worksheet at the specified index and populates it with values.
    Widens the worksheet if there are more values than columns.
    :param worksheet: The worksheet to be updated.
    :param index: Index of the row to be updated.
    :param values: List of values for the row.
    """
    data_width = len(values)
    if worksheet.col_count < data_width:
        worksheet.resize(cols=data_width)

    cell_list = []
    for i, value in enumerate(values, start=1):
        cell = worksheet.cell(index, i)
        cell.value = value
        cell_list.append(cell)
        
    worksheet.update_cells(cell_list)
        
        
class SheetsClient(gspread.client.Client):
    """An instance of this class communicates with Google Data API."""

    AUTH_SCOPE = ' '.join(['https://docs.google.com/feeds/',
                           'https://spreadsheets.google.com/feeds/',
                           'https://www.googleapis.com/auth/drive.file'])

    DRIVE_API_URL = 'https://www.googleapis.com/drive/v2/files'
    
    def new(self, title):
        headers = {'Content-Type': 'application/json'}
        data = {
            'title': title, 
            'mimeType': 'application/vnd.google-apps.spreadsheet'
        }
        r = self.session.request(
            'POST', SheetsClient.DRIVE_API_URL, headers=headers, data=json.dumps(data))
        resp = json.loads(r.read().decode('utf-8'))
        sheet_id = resp['id']
        return self.open_by_key(sheet_id)


    def add_service_account_to_spreadsheet(self, spreadsheet):
        url = '%s/%s/permissions' % (SheetsClient.DRIVE_API_URL, spreadsheet.id)
        headers = {'Content-Type': 'application/json'}
        data = {
            'role': 'writer',
            'type': 'user',
            'value': settings.GOOGLE_CLIENT_EMAIL
        }

        self.session.request(
            'POST', url, headers=headers, data=json.dumps(data))

    @classmethod
    def login_with_service_account(cls):
        credential = SignedJwtAssertionCredentials(settings.GOOGLE_CLIENT_EMAIL,
                        settings.GOOGLE_CLIENT_PRIVATE_KEY, scope=SheetsClient.AUTH_SCOPE)

        client = SheetsClient(auth=credential)
        client.login()
        return client

    @classmethod
    def login_with_auth_token(cls, token_string):
        # deserialize the token.
        token = gdata.gauth.token_from_blob(token_string)
        assert token.refresh_token

        # Refresh OAuth token if necessary.
        oauth2_token = gdata.gauth.OAuth2Token(
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scope=SheetsClient.AUTH_SCOPE,
            user_agent='formhub')
        oauth2_token.refresh_token = token.refresh_token
        refreshed_token = get_refreshed_token(oauth2_token)

        # Create Google Sheet.
        client = SheetsClient(auth=refreshed_token)
        client.login()
        return client


class SheetsExportBuilder(ExportBuilder):
    client = None
    spreadsheet = None
    # Worksheets generated by this class.
    worksheets = {}
    # Map of section_names to generated_names
    worksheet_titles = {}
    # The URL of the exported sheet.
    url = None
    
    # Configuration options
    spreadsheet_title = None
    flatten_repeated_fields = True
    export_xlsform = True
    google_token = None
    
    # Constants
    SHEETS_BASE_URL = 'https://docs.google.com/spreadsheet/ccc?key=%s&hl'
    FLATTENED_SHEET_TITLE = 'raw'
    
    def __init__(self, xform, config):
        super(SheetsExportBuilder, self).__init__(xform, config)
        self.spreadsheet_title = config['spreadsheet_title']
        self.google_token = config['google_token']
        self.flatten_repeated_fields = config['flatten_repeated_fields']
        self.export_xlsform = config['export_xlsform']
   
    def export(self, path, data, username, id_string, filter_query):
        self.client = SheetsClient.login_with_auth_token(self.google_token)
        
        # Create a new sheet
        self.spreadsheet = self.client.new(title=self.spreadsheet_title)
        self.url = self.SHEETS_BASE_URL % self.spreadsheet.id
        
        # Add Service account as editor
        self.client.add_service_account_to_spreadsheet(self.spreadsheet)

        # Perform the actual export
        if self.flatten_repeated_fields:
            self.export_flattened(path, data, username, id_string, filter_query)
        else:
            self.export_tabular(path, data)
         
        # Write XLSForm data
        if self.export_xlsform:
            self._insert_xlsform()
        
        # Delete the default worksheet if it exists
        # NOTE: for some reason self.spreadsheet.worksheets() does not contain
        #       the default worksheet (Sheet1). We therefore need to fetch an 
        #       updated list here.
        feed = self.client.get_worksheets_feed(self.spreadsheet)
        for elem in feed.findall(gspread.ns._ns('entry')):
            ws = gspread.Worksheet(self.spreadsheet, elem)
            if ws.title == 'Sheet1':
                self.client.del_worksheet(ws)
           
    def export_flattened(self, path, data, username, id_string, filter_query): 
        # Build a flattened CSV
        from onadata.apps.viewer.pandas_mongo_bridge import CSVDataFrameBuilder
        csv_builder = CSVDataFrameBuilder(
            username, id_string, filter_query, self.group_delimiter,
            self.split_select_multiples, self.binary_select_multiples)
        csv_builder.export_to(path)
        
        # Read CSV back in and filter n/a entries
        rows = []
        with open(path) as f:
            reader = csv.reader(f)
            for row in reader:
                filtered_rows = [x if x != 'n/a' else '' for x in row]
                rows.append(filtered_rows)
        
        # Create a worksheet for flattened data
        num_rows = len(rows)
        if not num_rows:
            return
        num_cols = len(rows[0])
        ws = self.spreadsheet.add_worksheet(
            title=self.FLATTENED_SHEET_TITLE, rows=num_rows, cols=num_cols)
     
        # Write data row by row                      
        for index, values in enumerate(rows, 1):
            update_row(ws, index, values)
                    
    def export_tabular(self, path, data):        
        # Add worksheets for export.
        self._create_worksheets()
        
        # Write the headers
        self._insert_headers()

        # Write the data
        self._insert_data(data)
    
    def _insert_xlsform(self):
        """Exports XLSForm (e.g. survey, choices) to the sheet."""
        assert self.client
        assert self.spreadsheet
        assert self.xform
        
        file_path = self.xform.xls.name
        default_storage = get_storage_class()()
    
        if file_path == '' or not default_storage.exists(file_path):
            # No XLS file for your form
            return
        
        with default_storage.open(file_path) as xlsform_file:
            if file_path.endswith('.csv'):
                xlsform_io = convert_csv_to_xls(xlsform_file.read())
            else:
                xlsform_io = io.BytesIO(xlsform_file.read())
            # Open XForm and copy sheets to Google Sheets.
            workbook = xlrd.open_workbook(file_contents=xlsform_io.read())
            for wksht_nm in workbook.sheet_names():
                source_worksheet = workbook.sheet_by_name(wksht_nm)
                num_cols = source_worksheet.ncols
                num_rows = source_worksheet.nrows
                destination_worksheet = self.spreadsheet.add_worksheet(
                    title=wksht_nm, rows=num_rows, cols=num_cols)
                for row in xrange(num_rows):
                    update_row(destination_worksheet, row + 1,
                               [source_worksheet.cell_value(row, col) 
                                for col in xrange(num_cols)] )            
    
    def _insert_data(self, data):
        """Writes data rows for each section."""
        indices = {}
        survey_name = self.survey.name
        for index, d in enumerate(data, 1):
            joined_export = ExportBuilder.dict_to_joined_export(
                d, index, indices, survey_name)
            output = ExportBuilder.decode_mongo_encoded_section_names(
                joined_export)
            # attach meta fields (index, parent_index, parent_table)
            # output has keys for every section
            if survey_name not in output:
                output[survey_name] = {}
            output[survey_name][INDEX] = index
            output[survey_name][PARENT_INDEX] = -1
            for section in self.sections:
                # get data for this section and write to xls
                section_name = section['name']
                fields = [
                    element['xpath'] for element in
                    section['elements']] + self.EXTRA_FIELDS

                ws = self.worksheets[section_name]
                # section might not exist within the output, e.g. data was
                # not provided for said repeat - write test to check this
                row = output.get(section_name, None)
                if type(row) == dict:
                    SheetsExportBuilder.write_row(
                        self.pre_process_row(row, section),
                        ws, fields, self.worksheet_titles)
                elif type(row) == list:
                    for child_row in row:
                        SheetsExportBuilder.write_row(
                            self.pre_process_row(child_row, section),
                            ws, fields, self.worksheet_titles)
            
    def _insert_headers(self):
        """Writes headers for each section."""
        for section in self.sections:
            section_name = section['name']
            headers = [
                element['title'] for element in
                section['elements']] + self.EXTRA_FIELDS
            # get the worksheet
            ws = self.worksheets[section_name]
            update_row(ws, index=1, values=headers)
            
    def _create_worksheets(self):
        """Creates one worksheet per section."""
        for section in self.sections:
            section_name = section['name']
            work_sheet_title = self.get_valid_sheet_name(
                "_".join(section_name.split("/")), 
                self.worksheet_titles.values())
            self.worksheet_titles[section_name] = work_sheet_title
            num_cols = len(section['elements']) + len(self.EXTRA_FIELDS)
            self.worksheets[section_name] = self.spreadsheet.add_worksheet(
                title=work_sheet_title, rows=1, cols=num_cols)

    @classmethod    
    def write_row(cls, data, worksheet, fields, worksheet_titles):
        # update parent_table with the generated sheet's title
        data[PARENT_TABLE_NAME] = worksheet_titles.get(
            data.get(PARENT_TABLE_NAME))
        worksheet.append_row([data.get(f) for f in fields])
