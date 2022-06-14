
import os
from sqlalchemy.dialects.postgresql import insert
import sqlalchemy as s
import pandas as pd
import json
import httpx

from .db_models import *
from sqlalchemy.event import listen
from sqlalchemy.event import listens_for
from .config import AugurConfig

from .random_key_auth import RandomKeyAuth
# from .engine import engine

# #Derek
# @s.event.listens_for(engine, "connect", insert=True)
# def set_search_path(dbapi_connection, connection_record):
#     existing_autocommit = dbapi_connection.autocommit
#     dbapi_connection.autocommit = True
#     cursor = dbapi_connection.cursor()
#     cursor.execute(
#         "SET SESSION search_path=public,augur_data,augur_operations,spdx")
#     cursor.close()
#     dbapi_connection.autocommit = existing_autocommit


#TODO: setup github headers in a method here.
#Encapsulate data for celery task worker api


#TODO: Test sql methods
class TaskSession(s.orm.Session):

    #ROOT_AUGUR_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    def __init__(self,logger,config={},platform='github'):
        self.logger = logger
        
        current_dir = os.getcwd()

        self.root_augur_dir = ''.join(current_dir.partition("augur/")[:2])
        self.__init_config(self.root_augur_dir)
        
        DB_STR = f'postgresql://{self.config["user_database"]}:{self.config["password_database"]}@{self.config["host_database"]}:{self.config["port_database"]}/{self.config["name_database"]}'

        self.config.update(config)
        self.platform = platform
        
        #print(f"path = {str(ROOT_AUGUR_DIR) + "augur.config.json"}")
        

        self.engine = s.create_engine(DB_STR)
        # self.engine = engine
        
        keys = get_list_of_oauth_keys(self.engine, config["Database"]["key"])

        self.oauths = RandomKeyAuth(keys)


        super().__init__(self.engine)

    def __init_config(self, root_augur_dir):
        #Load config.
        self.augur_config = AugurConfig(self.root_augur_dir)
        self.config = {
            'host': self.augur_config.get_value('Server', 'host')
        }
        self.config.update(self.augur_config.get_section("Logging"))

        self.config.update({
            'capture_output': False,
            'host_database': self.augur_config.get_value('Database', 'host'),
            'port_database': self.augur_config.get_value('Database', 'port'),
            'user_database': self.augur_config.get_value('Database', 'user'),
            'name_database': self.augur_config.get_value('Database', 'name'),
            'password_database': self.augur_config.get_value('Database', 'password'),
            'key_database' : self.augur_config.get_value('Database', 'key')
        })

        print(self.config)

    
    @property
    def access_token(self):
        try:
            return self.__oauths.get_key()
        except:
            self.logger.error("No access token in queue!")
            return None

    
    def execute_sql(self, sql_text):
        connection = self.engine.connect()

        return connection.execute(sql_text)
    
    def insert_data(self, data, table, natural_keys):

        if len(data) == 0:
            return

        first_data = data[0]

        if type(first_data) == dict:
            self.insert_dict_data(data, table, natural_keys)
        else:
            self.insert_github_class_objects(data, table, natural_keys)

    def insert_dict_data(self, data, table, natural_keys):

        print(f"Length of data to insert: {len(data)}")

        self.logger.info(f"Length of data to insert: {len(data)}")
        self.logger.info(type(data))

        if type(data) != list:
            print("Data must be a list")
            return

        if type(data[0]) != dict:
            print("Data must be a list of dicts")
            self.logger.info("Must be list of dicts")
            return

        table_stmt = insert(table)

        for value in data:
            insert_stmt = table_stmt.values(value)
            insert_stmt = insert_stmt.on_conflict_do_update(
                index_elements=natural_keys, set_=dict(value))

            self.execute_sql(insert_stmt)

    def insert_github_class_objects(self, objects, table, natural_keys):

        self.logger.info(f"Length of data to insert: {len(objects)}")
        self.logger.info(type(objects))

        if type(objects) != list:
            print("Data must be a list")
            return

        table_stmt = insert(table)

        for obj in objects:
            data = obj.get_dict()
            insert_stmt = table_stmt.values(data)
            insert_stmt = insert_stmt.on_conflict_do_update(
                index_elements=natural_keys, set_=dict(data))

            self.execute_sql(insert_stmt)

            natural_key_dict = {}
            for key in natural_keys:
                natural_key_dict[key] = data[key]

            rows = table.query.filter_by(**natural_key_dict).all()

            if len(rows) > 1:
                print(f"Error values in table not unique on {natural_keys}")

            obj.set_db_row(rows[0])



    #TODO: Bulk upsert
    
    def insert_bulk_data(self,data,table,natural_keys):
        self.logger.info(f"Length of data to insert: {len(data)}")
        self.logger.info(type(data))

        if type(data) != list:
            self.logger.info("Data must be a list")
            return

        if type(data[0]) != dict:
            self.logger.info("Must be list of dicts")
            return

        stmnt = insert(table).values(data)

        setDict = {}
        for key in data.keys:
            setDict[key] = getattr(stmnt.excluded,key)

        stmnt = stmnt.on_conflict_do_update(
            #This might need to change
            index_elements=natural_keys,
            
            #Columns to be updated
            set_ = setDict
        )

        self.execute(stmnt)


def get_list_of_oauth_keys(db_engine, config_key):

    oauthSQL = s.sql.text(f"""
            SELECT access_token FROM augur_operations.worker_oauth WHERE access_token <> '{config_key}' and platform = 'github'
            """)

    oauth_keys_list = [{'access_token': config_key}] + json.loads(
        pd.read_sql(oauthSQL, db_engine, params={}).to_json(orient="records"))

    key_list = [x["access_token"] for x in oauth_keys_list]

    with httpx.Client() as client:

        # loop throuh each key in the list and get the rate_limit and seconds_to_reset
        # then add them either the fresh keys or depleted keys based on the rate_limit
        for key in key_list:

            key_data = get_oauth_key_data(client, key)

            # this makes sure that keys with bad credentials are not used
            if key_data is None:
                key_list.remove(key)

    return key_list


def get_oauth_key_data(client, oauth_key):

    # this endpoint allows us to check the rate limit, but it does not use one of our 5000 requests
    url = "https://api.github.com/rate_limit"

    headers = {'Authorization': f'token {oauth_key}'}

    response = client.request(
        method="GET", url=url, headers=headers, timeout=180)

    data = response.json()

    try:
        if data["message"] == "Bad credentials":
            return None
    except KeyError:
        pass

    return True
