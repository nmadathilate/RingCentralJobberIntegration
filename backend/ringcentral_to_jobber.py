import os
import json
import time
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from pathlib import Path

import requests
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
import schedule 
#load env variables
load_dotenv()

#configuration 
@dataclass 
class Config:
     #RingCentral Settings 
    ringcentral_client_id: str = field(default_factory=lambda: os.getenv("RINGCENTRAL_CLIENT_ID", ""))
    ringcentral_client_secret: str = field(default_factory=lambda: os.getenv("RINGCENTRAL_CLIENT_SECRET", ""))
    ringcentral_jwt: str = field(default_factory=lambda: os.getenv("RINGCENTRAL_JWT", ""))
    ringcentral_server_url: str = field(default_factory=lambda: os.getenv("RINGCENTRAL_SERVER_URL", "https://platform.ringcentral.com"))

    #Jobber settings 
    jobber_client_id: str = field(default_factory=lambda: os.getenv("JOBBER_CLIENT_ID", ""))
    jobber_client_secret: str = field(default_factory=lambda: os.getenv("JOBBER_CLIENT_SECRET", ""))
    jobber_redirect_uri: str = field(default_factory=lambda: os.getenv("JOBBER_REDIRECT_URI", "http://localhost:8080/callback"))
    jobber_api_base: str = "https://api.getjobber.com/api"
    jobber_subdomain: str = field(default_factory=lambda: os.getenv("JOBBER_SUBDOMAIN", "example_domain"))
    #JOBBER_TOKEN_URL = "https://api.getjobber.com/oauth/token"
    #JOBBER_AUTH_URL = "https://api.getjobber.com/oauth/authorize"
    #JOBBER_API_TOKEN = None 

    #App settings 
    check_interval_minutes: int = field(default_factory=lambda: int(os.getenv("CHECK_INTERVAL_MINUTES", 5)))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "./data"))
    mock_mode: bool = field(default_factory=lambda: os.getenv("MOCK_MODE", "false").lower() == "true")

    webhook_base_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_BASE_URL", "https://examplewebsite.com"))
    webhook_validation_token: str = field(default_factory=lambda: os.getenv("WEBHOOK_VALIDATION_TOKEN", "your-secure-token"))
    notification_port : int = field(default_factory=lambda: int(os.getenv("NOTIFICATION_PORT", "5001")))


    def __post_init__(self):
        "Validate the configuration"
        if not self.mock_mode:
            required_fields = [
                "ringcentral_client_id", "ringcentral_client_secret", "ringcentral_jwt",
                "jobber_client_id", "jobber_client_secret"
            ]
            missing = [field for field in required_fields if not getattr(self, field)]
            if missing:
                raise ValueError(f"Missing required configuration: {', '.join(missing)}")

#initialize config             
config = Config()

#setup the logging 
def setup_logging():
    "Application logging"
    log_dir = Path(config.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_dir / f"ringcentral_jobber{datetime.now().strftime('%Y%m%d')}.log"),
            logging.StreamHandler()
        ]

    )
    return logging.getLogger(__name__)

logger = setup_logging()

#Setup the database
class DatabaseManager:
    "SQLite3 database for tracking calls and tokens"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        "initialize the database tables"
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path)as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed_calls (
                               call_id TEXT PRIMARY KEY,
                               phone_number TEXT,
                               customer_id TEXT,
                               processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                               recording_url TEXT,
                               duration INTEGER,
                               call_start_time TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS api_tokens (
                               service TEXT PRIMARY KEY,
                               access_token TEXT,
                               refresh_token TEXT,
                               expires_at TIMESTAMP,
                               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sync_log (
                               id INTEGER PRIMARY KEY AUTOINCREMENT,
                               sync_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                               calls_processed INTEGER,
                               errors INTEGER,
                               status TEXT               
                );
                CREATE TABLE IF NOT EXISTS call_notifications (
                               call_id TEXT PRIMARY KEY,
                               from_number TEXT,
                               to_number TEXT,
                               caller_name TEXT,
                               start_time TIMESTAMP,
                               status TEXT,
                               customer_id TEXT,
                               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
            conn.commit() 
            
    def is_call_processed(self, call_id: str) -> bool:
        #check if the call has been processed yet 
        with sqlite3.connect(self.db_path) as conn:
            try:
                cursor = conn.execute("SELECT 1 FROM processed_calls WHERE call_id = ?", (call_id,))
                result = cursor.fetchone()
                return result is not None
            finally:
                cursor.close()
    
    def mark_call_processed(self, call_data: Dict):
        #Marks a call as processed 
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(""" 
                    INSERT OR REPLACE INTO processed_calls
                         (call_id, phone_number, customer_id, recording_url, duration, call_start_time)
                         VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    call_data['id'],
                    call_data.get('phone_number'),
                    call_data.get('customer_id'),
                    call_data.get('recording_url'),
                    call_data.get('duration'),
                    call_data.get('start_time')
                ))
                conn.commit()
            except Exception as e:
                conn.rollback() #rollback error 
                raise
    
    def store_token(self, service: str, token_data:Dict):
        #Stores the API token
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO api_tokens
                         (service, access_token, refresh_token, expires_at, updated_at)
                         VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    service,
                    token_data.get('access_token'),
                    token_data.get('refresh_token'),
                    token_data.get('expires_at')
                ))
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise

    def get_token(self, service:str) -> Optional[Dict]:
        "Retrieve API Token"
        with sqlite3.connect(self.db_path) as conn:
            try:
                cursor = conn.execute("""
                    SELECT access_token, refresh_token, expires_at
                                  FROM api_tokens WHERE service = ?
            """, (service,))
                row = cursor.fetchone()
                if row:
                    return {
                        'access_token': row[0],
                        'refresh_token': row[1],
                        'expires_at': row[2]
                }
            
                return None
            finally:
                cursor.close()
    
    def log_sync_result(self, calls_processed: int, errors: int, status: str):
        #Log sync operation results 
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    INSERT INTO sync_log (calls_processed, errors, status)
                    VALUES (?, ?, ?)
                """, (calls_processed, errors, status))
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise
    
    def store_call_notifications(self, call_data: Dict):
        #Stores call notification for real time tracking 
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO call_notifications
                    (call_id, from_number, to_number, caller_name, start_time, status, customer_id)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    call_data['call_id'],
                    call_data['from_number'],
                    call_data['to_number'],
                    call_data['caller_name'],
                    call_data['start_time'],
                    call_data['status'],
                    call_data['customer_id']
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Error storing call notifications: {e}")

    
    def close(self):
        #Cleanup method for tests
        pass 

#initialize the database 
db_manager = DatabaseManager(Path(config.data_dir) / "app.db")

@dataclass
class IncomingCall:
    #Incoming call information
    call_id: str
    from_number: str
    to_number: str
    caller_name: Optional[str]
    start_time: datetime
    status: str
    customer_info: Optional[Dict] = None

    def to_dict(self):
        #Converts to a dictionary
        data = asdict(self)
        data['start_time'] = self.start_time.isoformat()
        return  data
    


class RingCentralClient:
    def __init__(self):
        self.base_url = config.ringcentral_server_url
        self.access_token = None
    
    def authenticate(self) -> bool:
        #use JWT for authentication 
        if config.mock_mode:
            logger.info("[MOCK MODE] Skipping RingCentral authentication")
            self.access_token = "mock_access_token"
            return True
        try:
            url = f"{self.base_url}/restapi/oauth/token"
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": config.ringcentral_jwt
                }
            auth = (config.ringcentral_client_id, config.ringcentral_client_secret)

            response = requests.post(url, headers=headers, data=data, auth=auth, timeout=30)
            response.raise_for_status()

            token_data = response.json()
            self.access_token = token_data['access_token']

            #store the token with the expiration 
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data.get('expires_in', 3600))
            db_manager.store_token('ringcentral', {
                'access_token': self.access_token,
                'expires_at' : expires_at.isoformat()
            })

            logger.info("Successfully authenticated with RingCentral")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to authenticate with RingCentral: {e}")
            return False
        
    def get_recent_calls(self, days: int = 1) -> List[Dict]:
        if config.mock_mode:
            logger.info("[MOCK MODE] Returning mock call data")
            return [{
                "id": "mock_call_123",
                "startTime": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                "duration": 180,
                "from": {"phoneNumber": "+15551234567"},
                "recording": {"contentUri": "/restapi/v1.0/account/~/recording/123456789/content"} 
            }]
        if not self.access_token:
            if not self.authenticate():
                return []
        
        try:
            url = f"{self.base_url}/restapi/v1.0/account/~/call-log"
            headers = {"Authorization": f"Bearer {self.access_token}"}

            date_from = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            params = {
                "dateFrom": date_from,
                "withRecording": True,
                "perPage": 100
            }

            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            calls = data.get("records", [])

            logger.info(f"Retrieved {len(calls)} calls from RingCentral")
            return calls 
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to retrieve calls from RingCentral: {e}")
            return []
        
    def get_recording_url(self, content_uri: str) -> str:
        #Generates the full recording url
        if content_uri.startswith('http'):
            return content_uri
        return f"{self.base_url}{content_uri}"

class JobberClient:
#Jobber API client with OAuth2
    def __init__(self):
        self.base_url = config.jobber_api_base
        self.access_token = None
        self._load_stored_token()

    def _load_stored_token(self):
        token_data = db_manager.get_token('jobber')
        if token_data:
            self.access_token = token_data['access_token'] 
    
    def store_token(self, token_data: Dict):
        self.access_token = token_data['access_token']

        expires_at = None

        if 'expires_in' in token_data:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=token_data['expires_in'])).isoformat()

            db_manager.store_token('jobber', {
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'expires_at': expires_at
            })

            logger.info("Jobber token stored successfully")

    def find_customer_by_phone(self, phone_number: str) -> Optional[Dict]:
        #Find Jobber customer by searching through phone numbers 

        if config.mock_mode:
            logger.info(f"[MOCK MODE] Mock customer lookup for {phone_number}")
            return {"id": "mock_customer_123", "name": "Mock Customer" }
        
        if not self.access_token:
            logger.warning("No Jobber access token available")
            return None
        
        try:
            #clean the phone number to search 
            clean_phone = ''.join(filter(str.isdigit, phone_number))

            url = f"{self.base_url}/graphql"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }

            query = """
            query SearchClients($phone: String!) {
            clients(first:10, searchTerm: $phone) {
                nodes {
                id
                name
                phoneNumbers{
                    number
                }
                }
            }
            }"""

            payload = {
                "query": query,
                "variables": {"phone": clean_phone}
            }
            

            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

            data = response.json()
            clients = data.get('data', {}).get('clients', {}).get('nodes', [])

            #find the phone match 
            for client in clients:
                for phone in client.get('phoneNumbers', []):
                    client_phone = ''.join(filter(str.isdigit, phone.get('number', '')))
                    if client_phone == clean_phone:
                        logger.info(f"Found customer: {client['name']} for phone {phone_number}")
                        return client
            logger.info(f"No customer found for phone numer: {phone_number}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to search customer in Jobber: {e}")
            return None
    
    def create_customer_note(self, customer_id: str, note_content: str) -> bool:
        #Creates a note for the specific customer in Jobber 

        if config.mock_mode:
            logger.info(f"[MOCK MODE] Would create note for customer {customer_id}")
            return True
        
        if not self.access_token:
            logger.warning("No Jobber access token available")
            return False
        
        try:
            url = f"{self.base_url}/graphql"
            headers = {
                "Authorication": f"Bearer {self.access_token}",
                "Content-Type": "application//json"
            }

            mutation = """
            mutation CreateNote($clientId: ID!, $body: String!){
            noteCreate(input: {
                clientId: $clientId,
                body: $body
            }) {
            note {
                id
                body
            }
            userErrors {
                field 
                message
            }
            }
            }"""

            payload = {
                "query": mutation,
                "variables": {
                    "clientId": customer_id,
                    "body": note_content
                }
            }
            

            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

            data = response.json()
            errors = data.get('data', {}).get('noteCreate', {}).get('userErrors', [])

            if errors:
                logger.error(f"Failed to create note: {errors}")
                return False
            logger.info(f"Successfully created note for customer {customer_id}")
            return True
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create note in jobber {e}")
            return False

class CallNotificationManager:

    def __init__(self, jobber_client: JobberClient):
        self.active_calls: Dict[str, IncomingCall] = {}
        self.jobber = jobber_client
        self.webhook_subscription_id = None
    
    def setup_ringcentral_webhooks(self, ringcentral_client: RingCentralClient):
        if config.mock_mode:
            logger.info("[MOCK MODE] Skipping webhook setup")
            return {"id": "mock_webhook_123"}
        
        try:
            event_filters = [
                '/restapi/v1.0/account/~extenstion/~/telephony/sessions',
                '/restapi/v1.0/account/~extension/~/presence?detailedTelephonyState=true'
            ]
            webhook_data = {
                'eventFilters': event_filters,
                'deliveryMode': {
                    'transportType': 'WebHook',
                    'address': f"{config.webhook_base_url}/ringcentral/webhook",
                    'validationToken': config.webhook_validation_token
                },
                'expiresIn': 86400 * 365 #1yr
            }
            headers = {
                'Authorization': f'Bearer {ringcentral_client.access_token}',
                'Content-Type': 'application/json'
            }

            response = requests.post(
                f"{ringcentral_client.base_url}/restapi/v1.0/subscription",
                headers=headers,
                json=webhook_data,
                timeout=30
            )
            if response.status_code == 200:
                subscription_data = response.json()
                self.webhook_subscription_id = subscription_data['id']
                logger.info(f"Webhook registered successfuly: {self.webhook_subscription_id}")
                return subscription_data
            else:
                logger.error(F"Failed to register webhook: {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error setting up webhooks: {e}")
            return None
    def process_call_events(self, event_data):
        try:
            if 'body' in event_data:
                body = event_data['body']

                if 'telephonySessionsEvent' in body:
                    sessions = body['telephonySessionsEvent'].get('telephonySessions', [])
                    for session in sessions:
                        self._handle_telephony_session(session)
        except Exception as e:
            logger.error(f"Error processing call event: {e}")
    
    def _handle_telephony_session(self, session_data):
        session_id = session_data.get('id')
        status = session_data.get('status', '')

        if status == 'Proceeding' and session_data.get('direction') == 'Inbound':
            #call is happening right now 
            self._handle_incoming_call(session_data)
        elif status in ['Disconnected', 'Finished']:
            self._handle_call_ended(session_id)

    def _handle_incoming_call(self, session_data):
        #Handles a new incoming call
        call_id = session_data['id']
        from_number = session_data.get('from', {}).get('phoneNumber', '')
        to_number = session_data.get('to', {}).get('phoneNumber', '')

        customer_info = self.lookup_customer_in_jobber(from_number)
        incoming_call = IncomingCall(
            call_id=call_id,
            from_number=from_number,
            to_number=to_number,
            caller_name=customer_info.get('name', 'Unknown Caller') if customer_info else 'Unknown Caller',
            start_time=datetime.now(timezone.utc),
            status='incoming',
            customer_info=customer_info

        )
        self.active_calls[call_id] = incoming_call
        logger.info(f"Incoming call from {from_number}: {incoming_call.caller_name}")

        #stores the call in the database for tracking
        db_manager.store_call_notifications({
            'call_id': call_id,
            'from_number': from_number,
            'to_number': to_number,
            'caller_name': incoming_call.caller_name,
            'start_time': incoming_call.start_time,
            'status': 'incoming',
            'customer_id': customer_info.get('id') if customer_info else None
        })

    def _handle_call_ended(self, call_id):
        #Handle call ending
        if call_id in self.active_calls:
            call = self.active_calls[call_id]
            call.status = 'ended'
            logger.info(f"Call ended: {call.from_number}")
            #Keep in active calls for a short time for dashboard display 
            timer = threading.Timer(30.0, lambda: self.active_calls.pop(call_id, None))
            timer.daemon = True
            timer.start()
    
    def lookup_customer_in_jobber(self, phone_number):
        #looks up the customers info in jobber 
        if config.mock_mode:
            return{
                'id': 'mock_customer_123',
                'name': 'Test Customer',
                'phone': phone_number,
                'jobber_uri': f"https://{config.jobber_subdomain}.getjobber.com/clients/mock_customer_123"

            }
        try:
            #use existing jobber client to find customer
            customer = self.jobber.find_customer_by_phone(phone_number)

            if customer:
                return {
                    'id': customer['id'],
                    'name': customer.get('name', 'Unknown'),
                    'phone': phone_number,
                    'jobber_uri': f"https://{config.jobber_subdomain}.getjobber.com/clients/{customer['id']}"

                }
        except Exception as e:
            logger.error(f"Error looking up customer {phone_number}: {e}")
        
        return None
    
            #Calls ended 
#main service class 
class RingCentralJobberSync:
    def __init__(self):
        self.ringcentral = RingCentralClient()
        self.jobber = JobberClient()
        self.is_running = False
        self.sync_thread = None
        self.call_manager = CallNotificationManager(self.jobber)

    def start_sync_service(self):
        #Start automatic synchronization service
        if self.is_running:
            logger.warning("Sync service is already running")
            return 
        
        self.is_running = True

        #schedule periodic sync
        schedule.every(config.check_interval_minutes).minutes.do(self.sync_calls)

        #in separate thread check scheduler 
        self.sync_thread = threading.Thread(target=self._run_scheduler, daemon=True)
        self.sync_thread.start()

        logger.info(f"Sync service thread - checking every {config.check_interval_minutes} minutes")

        self.sync_calls()

    def stop_sync_service(self):
        #Stops synchronization
        self.is_running = False
        schedule.clear()
        logger.info("Sync service stopped")
    
    def _run_scheduler(self):
        #scheduler loop
        while self.is_running:
            schedule.run_pending()
            time.sleep(1)
    
    def sync_calls(self):
        #Sync recent calls from RingCentral to Jobber
        logger.info("Starting call synchronization")

        calls_processed = 0
        errors = 0

        try:
            #get the recent calls from RingCentral
            calls = self.ringcentral.get_recent_calls()

            for call in calls:
                try:
                    if self._process_call(call):
                        calls_processed += 1
                except Exception as e:
                    logger.error(f"Error processing call { call.get('id', 'unkown')}: {e}")
                    errors += 1

            status = "completed" if errors == 0 else "completed_with_errors"
            db_manager.log_sync_result(calls_processed, errors, status)

            logger.info(f"Sync completed: {calls_processed} calls processed, {errors} errors")
        except Exception as e:
            logger.error(f"Sync failed: {e}")
            db_manager.log_sync_result(0,1,"failed")

    def _process_call(self, call: Dict) -> bool:
        #Process a single call record
        call_id = call.get('id')

        #if already processed skip
        if db_manager.is_call_processed(call_id):
            return False
        
        recording = call.get('recording')
        if not recording:
            return False
        
        phone_number = call.get('from', {}).get('phoneNumber')
        if not phone_number:
            logger.warning(f"Call {call_id} has no phone number")
            return False
        
        customer = self.jobber.find_customer_by_phone(phone_number)
        if not customer:
            logger.info(f"No customer found for phone {phone_number}")
            return False
        
        recording_url = self.ringcentral.get_recording_url(recording.get('contentUri', ''))
        start_time = call.get('startTime', '')
        duration = call.get('duration', 0)

        note_content = self._format_call_note(phone_number, start_time, duration, recording_url)

        #create the note in jobber 
        if self.jobber.create_customer_note(customer['id'], note_content):
            db_manager.mark_call_processed({
                'id': call_id,
                'phone_number': phone_number,
                'recording_url': recording_url,
                'customer_id': customer['id'],
                'duration': duration,
                'start_time': start_time
            })
            
            logger.info(f"Processed call for customer: {customer.get('name', 'Unkown')}")
            return True
        
        return False
    
    def _format_call_note(self, phone: str, start_time: str, duration: int, recording_url: str) -> str:
        #used to format the note in jobber 
        try:
            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            formatted_date = dt.strftime('%B %d, %Y at %I:%M %p')
        except:
            formatted_date = start_time

        minutes, seconds = divmod(duration,60)
        duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

        return f""" Call Recording - {formatted_date}
Phone: {phone}
Duration: {duration_str}
Recording: {recording_url}

Note: Recording access requires RingCentral login credentials.
This not was automatically generated by the RingCentral-Jobber integration."""
    
    def start_notification_service(self, port=None):
        #Start real time notification service
        if port is None:
            port = config.notification_port
        
        webhook_result = self.call_manager.setup_ringcentral_webhooks(self.ringcentral)

        if webhook_result or config.mock_mode:
            dashboard_app = NotificationDashboardApp(self.call_manager, self)

            self.dashboard_thread = threading.Thread(
                target=lambda: dashboard_app.app.run(
                    host='0.0.0.0',
                    port=port,
                    debug=False,
                    use_reloader=False
                ),
                daemon=True
            )
            self.dashboard_thread.start()

            logger.info(f"Real-Time notification service started on port {port}")
            logger.info(f"Dashboard:http://localhost:{port}/dashboard")
            logger.info(f"Keep dashboard open in a browser tab to monitor calls")

            return True
        else:
            logger.error("Failed to start notification service - webhook setup failed")
            return False
class NotificationDashboardApp:
    #Flask app for notification dashboard

    def __init__(self, call_manager: CallNotificationManager, sync_service):
        self.app = Flask(__name__)
        self.call_manager = call_manager
        self.sync_service = sync_service
        self.setup_routes()

    def setup_routes(self):
        #Sets up flask routes
        @self.app.route('/ringcentral/webhook', methods=['POST'])
        def handle_ringcentral_webhook():
            #Handle incoming RingCentral webhook notifications
            #Validate token for security
            validation_token = request.headers.get("Validation-Token")
            if validation_token and validation_token != config.webhook_validation_token:
                logger.warning("Invalid webhook validation token")
                return jsonify({'error': 'Invalid validation token'}), 401
            
            if request.method == 'POST' and validation_token and not request.get_json(silent=True):
                response = jsonify({'validationToken': validation_token})
                response.headers['Validation-Token'] = validation_token
                return response
            try:
                event_data = request.get_json(silent=True)
                if event_data:
                    logger.debug(f"Received webhook: {json.dumps(event_data, indent=2)}")
                    self.call_manager.process_call_event(event_data)
                return jsonify({'status': 'success'}), 200
            except Exception as e:
                logger.error(f"Error processing webhook: {e}")
                return jsonify({'error': 'Processing Failed'}), 500
        
        @self.app.route('/dashboard')
        def notification_dashboard():
            #Real time call dashboard
            return render_template_string(self.get_dashboard_template())
        
        @self.app.route('/api/calls/active')
        def get_active_calls():
            calls = [call.to_dict() for call in self.call_manager.active_calls.values()]
            return jsonify({'calls': calls})
        
        @self.app.route('/api/calls/<call_id>/customer')
        def get_customer_jobber_link(call_id):
            #Get direct link to customer in Jobber
            call = self.call_manager.active_calls.get(call_id)
            if not call or not call.customer_info:
                return jsonify({'error': 'Customer not found'}), 404
            
            return jsonify({
                'customer': call.customer_info,
                'jobber_link': call.customer_info.get('jobber_uri')
            })
        
        @self.app.route('/api/calls/<call_id>/create-note', methods=['POST'])
        def create_call_note(call_id):
            #Create a note in jobber for this call
            call = self.call_manager.active_calls.get(call_id)
            if not call or not call.customer_info:
                return jsonify({'error': 'Call or customer not found'}), 404
            try:
                note_data = request.get_json(silent=True) or {}
                note_content = f"""Call Log - {call.start_time.strftime('%B %d, %Y at %I:%M %p')}
From: {call.from_number}
Customer: {call.caller_name}
Status: {call.status}

Notes: {note_data.get('note', 'No additional notes')}

This note was automatically generated by the RingCentral-Jobber integration."""
                if self.sync_service.jobber.create_customer_note(call.customer_info['id'], note_content):
                    return jsonify({'success': True, 'message': 'Note created successfully'})
                else:
                    return jsonify({'error': 'Failed to create note'}), 500
            except Exception as e: 
                logger.error(f"Error creating note; {e}")
                return jsonify({'error': 'Failed to create note'}), 500
            
        @self.app.route('/api/sync/status')
        def get_sync_status():
            #Get current sync service status
            return jsonify({
                'sync_running': self.sync_service.is_running,
                'webhook_active': self.call_manager.webhook_subscription_id is not None,
                'active_calls': len(self.call_manager.active_calls)
            })
    
    def get_dashboard_template(self):
        #HTML template for notifification dashboard
        return """
<!DOCTYPE html>
<html>
<head>
    <title>RingCentral Call Notificaltions</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f8f9fa;   
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .header {
            background: #007bff; color: white; padding: 20px;
            border-radius: 8px; margin-bottom: 20px; text-align: center;
        }
        .status-bar {
            background: #fff; padding: 15px; border-radius: 8px;
            margin-bottom: 20px; display: flex; justify-content: space-between;
            align-items: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .status-indicator {
            display: inline-flex; align-items: center;
            padding: 5px 10px; border-radius: 20px; font-size: 14px;
        }
        .status-active { background: #d4edda; color: #155724; }
        .status-inactive { background: #f8d7da; color: #721c24; }
        .call-grid {
            display: grid; gap: 20px;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
        }
        .call-card {
            background: #fff; border-radius: 8px; padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1); border-left: 4px solid #28a745;
            animation: slideIn 0.3s ease-out;
        }
        .call-card.ended { border-left-color: #6c757d; opacity: 0.7; }
        @keyframes slideIn {
            from { transform: translateY(-20px); opacity: 0; }
            to { transform: translateY(0); opacity: 1;}
        }
        .caller-info {
            display: flex; align-items: center; margin-bottom: 15px;
        }
        .caller-avatar {
        width: 50px; height: 50px; border-radius: 50%;
        background: #007bff; color: white; display: flex;
        align-items: center; justify-content: center;
        font-size: 20px; margin-right: 15px;
        }
        .caller-details h3 { margin: 0; font-size: 18px; }
        .caller-details p { margin: 5px 0 0 0; color: #6c757d; }
        .call-meta {
            display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
            margin-bottom: 15px; font-size: 14px;
        }
        .meta-item {
            background: #f8f9fa; padding: 8px; border-radius: 4px;
        }
        .meta-label { font-weight: 600; color: #495057; }
        .call-actions {
            display: flex; gap: 10px; flex-wrap: wrap;
        }
        .btn {
            padding: 10px 16px; border: none; border-radius: 6px;
            cursor: pointer; font-size: 14px; font-weight: 500;
            transition: all 0.2s; text-decoration: none; display: inline-block;
        }
        .btn-hover { transform: translateY(-1px); }
        .btn-primary { background: #007bff; color: white; }
        .btn-primary:hover { background: #0056b3; }
        .btn-success { background: #28a745; color: white; }
        .btn-success:hover { background: #1e7e34; }
        .no-calls {
            text-align: center; padding: 60px 20px; color: #6c757d; 
        }
        .notification-permission {
            background: #fff3cd; color: #856404; padding: 15px; 
            border-radius: 8px; margin-bottom: 20px; cursor: pointer;
        }
        @media (max-width: 768px) {
            .call-grid { grid-template-columns: 1fr; }
            .status-bar { flex-direction: column; gap: 10px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>RingCentral Call Notifications</h1>
            <p> Real-time incoming call monitoring for your team</p>
        </div>

        <div id="notification-permission" class="notification-permission" style="display: none;">
            <strong>Enable Desktop Notifications</strong><br>
            Click here to enable desktop notifications for incoming calls
        </div>

        <div class="status-bar">
            <div> 
                <span class="status-indicator" id="sync-status">
                    <span>●</span> Checking sync status ...
                </span>
                <span class="status-indicator" id="webhook-status">
                    <span>●</span> Checking webhook...
                </span>
            </div>
            <div>
                <strong>Active Calls: <span id="call-count">0</span></strong>
            </div>
        </div>

        <div id="call-notifications" class="call-grid">
            <div class="no-calls">
                <h3>Ready for calls!</h3>
                <p>This dashboard will show incoming calls in real-time.<br>
                Keep this tab open to monitor customer calls.</p>
            </div>
        </div>
    </div>

    <script>
        let notificationPermission = false;

        //check notification permission on load
        if ('Notification' in window){
            if (Notification.permission === 'granted') {
                notificationPermission = true;
            } else if (Notification.permission !== 'denied') {
                document.getElementById('notification-permission').style.display = 'block';
            }
        }
        //Request notification permission
        document.getElementById('notification-permission').onclick = function() {
            Notification.requestPermission().then(function(permission) {
                if (permission === 'granted') {
                    notificationPermission = true;
                    document.getElementById('notification-permission').style.display = 'none';

                }
            });
        };
        
        //update status indicators
        function updateStatus() {
        fetch('/api/sync/status')
            .then(r => r.json())
            .then(data => {
                const syncStatus = document.getElementById('sync-status');
                const webhookStatus = document.getElementById('webhook-status');

                if (data.sync_running) {
                    syncStatus.className = 'status-indicator status-active';
                    syncStatus.innerHTML = '<span>●</span> Sync Active';
                } else {
                    syncStatus.className = 'status-indicator status-inactive';
                    syncStatus.innerHTML = '<span>●</span> Sync Stopped';
                }

                if (data.webhook_active) {
                    webhookStatus.className = 'status-indicator status-active';
                    webhookStatus.innerHTML = '<span>●</span> Webhook Connected';
                } else {
                webhookStatus.className = 'status-indicator status-inactive';
                webhookStatus.innerHTML = '<span>●</span> Webhook Disconnected';
                }
            })
            .catch(err => console.error('Error fetching status:', err));
        }

        //update call display
        function updateCalls() {
        fetch('/api/calls/active')
            .then(r => r.json())
            .then(data => {
            displayCalls(data.calls);
            document.getElementById('call-count').textContent = data.calls.length;
            })
            .catch(err => console.error('Error fetching calls:', err));
        }

        function displayCalls(calls) {
            const container = document.getElementById('call-notifications');

            if (calls.length === 0){
                container.innerHTML = `
                    <div class="no-calls">
                        <h3>Ready for calls!</h3>
                        <p>This dashboard will show incoming calls in real-time.<br>
                        Keep this tab open to monitor customer calls.</p>
                    </div>
                `;
                return;
            }

            container.innerHTML = calls.map(call => {
                const callTime = new Date(call.start_time).toLocaleTimeString();
                const isEnded = call.status === 'ended';
                const customerName = call.caller_name || 'Unknown Caller';
                const initials = customerName.split(' ').map(n => n[0]).join('').toUpperCase();

                return `
                    <div class="call-card ${isEnded ? 'ended' : ''}">
                        <div class="caller-info">
                            <div class="caller-avatar">${initials}</div>
                            <div class="caller-details">
                                <h3>${customerName}</h3>
                                <p>${call.from_number}</p>
                            </div>
                        </div>

                        <div class="call-meta">
                            <div class="meta-item">
                                <div class="meta-label">Start Time</div>
                                ${callTime}
                            </div>
                            <div class="meta-item">
                                <div class="meta-label">Status</div>
                                ${call.status.charAt(0).toUpperCase() + call.status.slice(1)}
                            </div>
                            ${call.customer_info ? `
                                <div class = "meta-item">
                                    <div class="meta-label">Customer ID</div>
                                    ${call.customer_info.id}
                                </div>
                            ` : ''}
                        </div>

                        ${call.customer_info ? `
                            <div class="call-actions">
                                <button class="btn btn-primary" onclick="openJobberCustomer('${call.call_id}')">
                                    Open in Jobber
                                </button>
                                <button class="btn btn-success" onclick="createNote('${call.call_id}')">
                                    Create Note
                                    </button>
                            </div>
                        ` : `
                            <div class="call-actions">
                                <span style="color: #6c757d; font-style:italic;">
                                    Customer not found in Jobber
                                </span>
                            </div>
                        `}
                    </div>
                ';
                            
            }).join('');

            //show desktop notification for new calls 
            const newCalls = calls.filter(call => call.status === 'incoming');
            newCalls.forEach(call => {
                if (notificationPermission && !call.notified) {
                    new Notification(`Incoming Call: ${call.caller_name}`, {
                        body: `From: ${call.from_number}`,
                        icon: '/static/phone-icon.png'
                    });
                    call.notified = true;
                }
            });
        }

        function openJobberCustomer(callId) {
            fetch(`/api/calls/${callId}/customer`)
                .then(r => r.json())
                .then(data => {
                    if (data.jobber_link) {
                        window.open(data.jobber_link, '_blank');
                    } else {
                        alert('Customer link not available');
                    }
                })
                .catch(err => {
                    console.error('error opening customer:', err);
                    alert('Error opening customer link');
                });
        }

        function createNote(callId) {
            const note = prompt('Add a note about this call:');
            if (note !== null) {
                fetch(`/api/calls/${callId}/create-note`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ note: note })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        alert('Note created successfully!');
                    } else {
                        alert('Error creating note: ' + data.error);
                    }
                })
                .catch(err => {
                    console.error('Error creating note:', err);
                    alert('Error creating note');
                });
            }

        }

        //Update every 2 seconds 
        setInterval(updateCalls, 2000);
        setInterval(updateStatus, 10000);

        //initial load
        updateStatus();
        updateCalls();

        //page visibility handling 
        document.addEventListener('visibilitychange', function() {
            if (!document.hidden) {
                updateCalls();
                updateStatus();
            }
        });
    </script>
</body>
</html>

    """
    
    #Flask application for OAuth and management interface 
def create_app():
    print("DEBUG: Starting create_app")
    app = Flask(__name__)
    print("DEBUG: Flask app created")
    try:
        sync_service = RingCentralJobberSync()
        print("DEBUG: SYNC SERVICE CREATED SUCCESSFULLY")
    except Exception as e :
        print(f"DEBUG: Error creating sync service: {e}")
        import traceback 
        traceback.print_exc()

    @app.route('/')
    def home():
        #Home page with authorization and status
        template = """
        <!DOCTYPE html>
        <html>
        <head>
            <title> RingCentral-Jobber Integration </title>
            <style>
                body{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }
                .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
                .status { padding: 15px; margin: 20px 0; border-radius: 4px; }
                .success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
                .warning { background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }
                .error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
                button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px; }
                button:hover { background: #0056b3; }
                .disabled { background: #6c757d; cursor: not-allowed; }
                .logs { background: #f8f9fa; padding: 15px; border-radius: 4px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 12px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1> RingCentral-Jobber Integration</h1>
                <p> This service automatically syncs call recordings from RingCentral to customer notes in Jobber. </p>

                <div id="status" class="status warning">
                    <strong>Status:</strong> <span id="status-text">Checking...</span>
                </div>

                <div class="actions">
                    <h3>Setup</h3>
                    <p>1. First authorize access to jobber: </p>
                    <button onclick="authorizeJobber()"> Authorize Jobber Access</button>

                    <p>2. Start the synchronization service</p>
                    <button id="start-btn" onclick="startSync()">Start Sync Service</button>
                    <button id="stop-btn" onclick="stopSync()" class="disabled"> Stop Sync Service</button>

                    <p>3. Manual Sync (for testing purposes): </p>
                    <button onclick="manualSync()">Run Manual Sync</button>
                    <p>4. Real Time Call Notifications: </p>
                    <button onclick="openNotificationDashboard()">Open Call Dashboard</button>
                </div>

                <div class="logs">
                    <h4>Recent Activity</h4>
                    <div id="logs">Loading...</div>
                </div>
            </div>

            <script>
                function authorizeJobber() {
                    const authUrl = '/auth/jobber';
                    window.open(authUrl, 'jobber_auth', 'width=600,height=400');

                }

                function startSync() {
                    fetch('/api/sync/start', {method: 'POST'})
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                document.getElementById('start-btn').classList.add('disabled');
                                document.getElementById('stop-btn').classList.remove('disabled');
                                updateStatus();
                        } else{
                            alert('Error starting sync: ' + (data.error || 'Unkown error'));
                        }
                        })
                        .catch(err => {
                        console.error('Error starting sync:', err);
                        alert('Error starting sync: ' + err.message)
                        
                    });
                }

                function stopSync() {
                    fetch('/api/sync/stop', {method: 'POST'})
                        .then(r => r.json())
                        .then(data => {
                            if (data.success){
                                document.getElementById('start-btn').classList.remove('disabled');
                                document.getElementById('stop-btn').classList.add('disabled');
                                updateStatus();
                    } else{
                        alert('Error stopping sync: ' + (data.error || 'Unknown error'));
                    }

                })
                .catch(err => {
                    console.error('Error stopping sync:', err);
                    alert('Error stopping sync: ' + err.message);
                });
                }

                function manualSync() {
                    fetch('/api/sync/manual', {method: 'POST'})
                        .then(r=> r.json())
                        .then(data=> alert(data.message || 'Sync completed'))
                        .catch(err => {
                        console.error('Error with manual sync:', err);
                        alert('Error with manual sync: ' + err.message);
                        });
                }

                function openNotificationDashboard() {
                    window.open('http://localhost:""" + str(config.notification_port) + """/dashboard', '_blank');
                }

                function updateStatus() {
                    fetch('/api/status')
                        .then(r => r.json())
                        .then(data => {
                            const statusEl = document.getElementById('status');
                            const statusText = document.getElementById('status-text');
                            statusText.textContent = data.status;
                            statusEl.className = 'status ' + (data.running ? 'success' : 'warning');
                            
                    })
                    .catch(err => {
                        console.error('Error fetching status:', err);
                    });
                }

                function updateLogs() {
                    fetch('/api/logs')
                        .then(r => r.json())
                        .then(data => {
                            document.getElementById('logs').innerHTML = data.logs.join('<br>');
                        })
                        .catch(err => {
                            console.error('Error fetching logs:', err);
                            document.getElementById('logs').innerHTML = 'Error loading logs';
                        });
                }

                //update status and logs periodically
                setInterval(updateStatus, 5000);
                setInterval(updateLogs, 10000);
                updateStatus();
                updateLogs();
            </script>
        </body>
        </html>
        """
        return template
    
    @app.route('/auth/jobber')
    def auth_jobber():
        #Redirect to Jobber OAuth

        auth_url = (
            f"https://api.getjobber.com/oauth/authorize"
            f"?response_type=code"
            f"&client_id={config.jobber_client_id}"
            f"&redirect_uri={config.jobber_redirect_uri}"
            f"&scope=clients:read clients:write"
        )
        print(f"DEBUG: OAuth url: {auth_url}")
        print(f"DEBUG: Redirect URI: {config.jobber_redirect_uri}")

        return f'<script>window.location.href="{auth_url}";</script>'
        
    @app.route('/callback')
    def oauth_callback():
        #Handles the oauth callback from Jobber
        code = request.args.get('code')
        error = request.args.get('error')
        print(f"DEBUG: callback called with args: {dict(request.args)}")
        print(f"DEBUG: Code: {code}")
        print(f"DEBUG: error: {error}")
        if not code:
            return jsonify({"error": "No authorization code received"}), 400
            
        try:
            token_url = "https://api.getjobber.com/oauth/token"
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.jobber_redirect_uri,
                "client_id": config.jobber_client_id,
                "client_secret": config.jobber_client_secret
            }

            response  = requests.post(token_url, data=data, timeout=30)
            response.raise_for_status()

            token_data = response.json()
            sync_service.jobber.store_token(token_data)

            return """
            <html>
            <body>
                <h2> Authorization Successful! </h2>
                <p>Jobber access has been granted. You can close this window and return to the main application. </p>
                    
                <script>
                    setTimeout(() => window.close(), 3000);
                </script>
            </body>
            </html>
            """
        except Exception as e:
            logger.error(f"OAuth callback error: {e}")
            return jsonify({"error": str(e)}), 500
        
    @app.route('/api/status')
    def api_status():
        #Get service status
        return jsonify({
            "running": sync_service.is_running,
            "status": "Running" if sync_service.is_running else "Stopped",
            "last_sync": "N/A" #Get this from the database later 
        })
        
    @app.route('/api/sync/start', methods=['POST'])
    def api_start_sync():
        #Start the synchronization service 
        try:
            sync_service.start_sync_service()
            if hasattr(sync_service, 'start_notification_service'):
                sync_service.start_notification_service()
            return jsonify({"success": True, "message": "Sync service has started"})
        except Exception as e:
            logger.error(f"Error starting sync service: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        
    @app.route('/api/sync/stop', methods=['POST'])
    def api_stop_sync():
        #Stop the sync service 
        try:
            sync_service.stop_sync_service()
            return jsonify({"success": True, "message": "Sync service terminated"})
        except Exception as e:
            logger.error(f"Error stopping sync service: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        
    @app.route('/api/sync/manual', methods=['POST'])
    def api_manual_sync():
        #run the manual sync (for testing)
        try: 
            sync_service.sync_calls()
            return jsonify({"success": True, "message": "Manual sync completed"})
        except Exception as e:
            logger.error(f"Error with manual sync: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        

    @app.route('/api/logs')
    def api_logs():
        #Get the log entries 
        try: 
            log_file = Path(config.data_dir) / "logs" / f"ringcentral_jobber_{datetime.now().strftime('%Y%m%d')}.log"
            if log_file.exists():
                with open(log_file, 'r') as f:
                    lines = f.readlines()
                    #Return the last 50 lines 
                    recent_logs = lines[-50:] if len(lines) > 50 else lines
                    return jsonify({"logs": [line.strip() for line in recent_logs]})
            return jsonify({"logs": ["No logs available"]})
        except Exception as e:
            logger.error(f"Error reading logs: {e}")
            return jsonify({"logs": [f"Error reading logs: {str(e)}"]})
    return app
    
def main(): 
    #main CLI entry point 
    import argparse

    parser = argparse.ArgumentParser(description="RingCentral to Jobber Integration Service")
    parser.add_argument('--mode', choices=['web', 'service', 'once', 'enhanced'], default='web',
                        help='Run mode: web interface, background service, or one-time sync, or enhanced with notifications')
    parser.add_argument('--port', type=int, default=8080,
                        help='Port for web interface (default: 8080)')
    parser.add_argument('--config', help='Path to configuration file')

    args = parser.parse_args()

    if args.config:
        load_dotenv(args.config)

    if args.mode == 'web':
        #run as a web interface
        app =create_app()
        logger.info(f"Starting web interface on port {args.port}")
        logger.info(f"Open http://localhost.{args.port} in your browser")
        app.run(host='0.0.0.0', port=args.port, debug=True)

    elif args.mode =='service':
        #run as a background service 
        logger.info("Starting background sync service")
        sync_service = RingCentralJobberSync()
        sync_service.start_sync_service()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            sync_service.stop_sync_service()
    
    elif args.mode == 'once':
        #do a one time sync
        logger.info("Running one-time sync")
        sync_service = RingCentralJobberSync()
        sync_service.sync_calls()
        logger.info("One-time sync completed")

    elif args.mode == 'enhanced':
        logger.info("Starting enhanced integration with real-time notifications")
        sync_service = RingCentralJobberSync()

        if not config.mock_mode:
            if not sync_service.ringcentral.authenticate():
                logger.error("Failed to authenticate with RingCentral")
                return 
            logger.info("RingCentral authentication successful")
        
        sync_service.start_sync_service()

        if sync_service.start_notification_service():
            logger.info("All services started successfully")

            print(f"""
Enhanced RingCentral-Jobber Integration is running!
Main DashBoard: http://localhost:{args.port}/
Call Notifications: http://localhost:{config.notification_port}/dashboard
Keep the notification dashboard open in a browser tab to monitor incoming calls.
Press Ctrl+C to stop all services.
                """)
            
            try:
                #keep main thread alive 
                while True:
                    time.sleep(60)
                    logger.info("System running - Active calls: " + 
                                str(len(sync_service.call_manager.active_calls)))
            except KeyboardInterrupt:
                logger.info("Shutting down services...")
                sync_service.stop_sync_service()
        else:
            logger.error("Failed to start notification service")

    

if __name__ == "__main__":
    main()

