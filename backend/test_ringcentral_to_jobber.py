import unittest
import requests
import os
from unittest import mock
from unittest.mock import patch, MagicMock, Mock
import tempfile 
import shutil
from datetime import datetime, timezone
import json 
import threading
import time
from flask import Flask
import tracemalloc



#ensures mock mode for testing 
os.environ['MOCK_MODE'] = 'true'

import ringcentral_to_jobber
from ringcentral_to_jobber import (
    Config, DatabaseManager, RingCentralClient, JobberClient,
    RingCentralJobberSync, setup_logging, IncomingCall, CallNotificationManager, NotificationDashboardApp

)


#tracemalloc.start()
class TestConfig(unittest.TestCase):
    #Test configuration management
    def setUp(self):
        self.original_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
    
    def test_config_with_defaults(self):
        #Test with default values
        os.environ.update({
            'MOCK_MODE': 'true',
            'RINGCENTRAL_CLIENT_ID': 'test_id',
            'RINGCENTRAL_CLIENT_SECRET': 'test_secret',
            'RINGCENTRAL_JWT': 'test_jwt',
            'JOBBER_CLIENT_ID': 'jobber_id',
            'JOBBER_CLIENT_SECRET': 'jobber_secret',
            'WEBHOOK_BASE_URL': 'https://test.ngrok.io',
            'WEBHOOK_VALIDATION_TOKEN': 'test-token-123',
            'NOTFICATION_PORT': '5002',
            'JOBBER_SUBDOMAIN': 'test_company'
        })

        config = Config()
        self.assertEqual(config.check_interval_minutes, 5)
        self.assertEqual(config.log_level, 'INFO')
        self.assertTrue(config.mock_mode)
        self.assertEqual(config.webhook_base_url, 'https://test.ngrok.io')
        self.assertEqual(config.webhook_validation_token, 'test-token-123')
        self.assertEqual(config.jobber_subdomain, 'test_company')
    

    def test_config_validation_missing_required(self):
        #TEst configuration validation with missing fields 
        os.environ.clear()

        os.environ['MOCK_MODE'] = 'false'
        
        with self.assertRaises(ValueError) as context:
            Config()
        self.assertIn('Missing required configuration:', str(context.exception))
    
    def test_config_validation_specific_missing_field(self):
        os.environ.clear()

        os.environ['MOCK_MODE'] = 'false'

        os.environ.update({
            'RINGCENTRAL_CLIENT_SECRET': 'test_secret',
            'RINGCENTRAL_JWT': 'test_jwt',
            'JOBBER_CLIENT_ID': 'jobber_id',
            'JOBBER_CLIENT_SECRET': 'jobber_secret'    
        })
        with self.assertRaises(ValueError) as context:
            Config()
        self.assertIn('Missing required configuration:', str(context.exception))

class TestDatabaseManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test_db')
        self.db_manager = DatabaseManager(self.db_path)

    def tearDown(self):
        if hasattr(self.db_manager, 'conn'):
            self.db_manager.conn.close()
        shutil.rmtree(self.temp_dir)
    
    def test_database_initialization(self):
        #makes sure the tables are created 
        self.assertTrue(os.path.exists(self.db_path))

        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            expected_tables = ['processed_calls', 'api_tokens', 'sync_log']
            for table in expected_tables:
                self.assertIn(table, tables)

    def test_call_processing_tracking(self):
        #Test call processing tracking
        call_data = {
            'id': 'test_call_123',
            'phone_number': '+15551234567',
            'customer_id': 'cust_456',
            'recording_url': 'http://example.com/recording',
            'duration': 180,
            'start_time': '2025-01-01T12:00:00Z' 
        }
        #Initially it is not proccessed 
        self.assertFalse(self.db_manager.is_call_processed('test_call_123'))

        #process the call
        self.db_manager.mark_call_processed(call_data)

        self.assertTrue(self.db_manager.is_call_processed('test_call_123'))

    def test_token_storage_retrieval(self):
        token_data = {
            'access_token': 'test_token_123',
            'refresh_token': 'refresh_456',
            'expires_at': '2025-12-31T23:59:59Z'
        }

        self.db_manager.store_token('test_service', token_data)

        retreived = self.db_manager.get_token('test_service')

        self.assertEqual(retreived['access_token'], 'test_token_123')
        self.assertEqual(retreived['refresh_token'], 'refresh_456')
        self.assertEqual(retreived['expires_at'],'2025-12-31T23:59:59Z')

    def test_sync_logging(self):
        #Test sync logging 
        self.db_manager.log_sync_result(5,1,'completed_with_errors')

        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT calls_processed, errors, status FROM sync_log ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()

            self.assertEqual(row[0], 5) #calls processed 
            self.assertEqual(row[1], 1) #errors 
            self.assertEqual(row[2], 'completed_with_errors') #status

class TestRingCentralClient(unittest.TestCase):
    #Test the RingCentral API Client
    def setUp(self):
        self.config = mock.Mock()
        self.config.MOCK_MODE = True
        self.client = RingCentralClient()
    
    def tearDown(self):
        pass

    @patch('ringcentral_to_jobber.config.mock_mode', True)
    def test_authentication_mock_mode(self):
        #test the auth in mock mode
        result = self.client.authenticate()
        self.assertTrue(result)
        self.assertEqual(self.client.access_token, "mock_access_token")

    @patch('requests.post')
    @patch('ringcentral_to_jobber.config.mock_mode', False)
    def test_authenticate_real_mode(self, mock_post):
        #Test with real API Call

        #mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {'access_token': 'real_token_123'}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        result = self.client.authenticate()

        self.assertTrue(result)
        self.assertEqual(self.client.access_token, 'real_token_123')
        mock_post.assert_called_once()

    @patch('requests.post')
    @patch('ringcentral_to_jobber.config.mock_mode', False)
    def test_authentication_failure(self, mock_post):
        mock_post.side_effect = requests.exceptions.RequestException("Network error")

        result = self.client.authenticate()
        self.assertFalse(result)
        self.assertIsNone(self.client.access_token)

    @patch('ringcentral_to_jobber.config.mock_mode', True)
    def test_get_recent_calls(self):
        #Test getting the recent calls 
        calls = self.client.get_recent_calls()

        self.assertIsInstance(calls, list)
        self.assertGreater(len(calls), 0)
        self.assertIn('recording', calls[0])
        self.assertIn('from', calls[0])
    
    def test_get_recording_url(self):
        #Test recording generation
        content_uri = "/restapi/v1.0/account/~/recording/123/content"
        expected_url = f"{self.client.base_url}{content_uri}"

        result = self.client.get_recording_url(content_uri)
        self.assertEqual(result, expected_url)


    def test_get_recording_url_full_url(self):
        #Test when there is already a full url 
        full_url = "https://media.ringcentral.com/recording/123"

        result = self.client.get_recording_url(full_url)
        self.assertEqual(result, full_url)

class TestJobberClient(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # next two 2 lines added for fixing
        self.config = mock.Mock()
        self.config.JOBBER_TOKEN_STORAGE = os.path.join(self.temp_dir, "token.json")
        self.db_path = os.path.join(self.temp_dir, 'test_db')

        with patch('ringcentral_to_jobber.db_manager') as mock_db:
            mock_db.get_token.return_value = None
            self.client = JobberClient()
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @patch('ringcentral_to_jobber.config.mock_mode', True)
    def test_find_customer_mock_mode(self):
        customer = self.client.find_customer_by_phone("+15551234567")

        self.assertIsNotNone(customer)
        self.assertIn('id', customer)
        self.assertIn('name', customer)

    @patch('requests.post')
    @patch('ringcentral_to_jobber.config.mock_mode', False)
    def test_find_customer_real_mode(self, mock_post):
        #Test finding a customer with a real API call
        self.client.access_token = "test_token"

        #Make successful response
        mock_response = Mock()
        mock_response.json.return_value = {
            'data': {
                'clients': {
                    'nodes': [{
                        'id': 'customer_123',
                        'name': 'Test Customer',
                        'phoneNumbers': [{'number': '+15551234567'}]
                    }]
                }
            }
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        customer = self.client.find_customer_by_phone("+15551234567")

        self.assertIsNotNone(customer)
        self.assertEqual(customer['id'], 'customer_123')
        self.assertEqual(customer['name'], 'Test Customer')

    @patch('requests.post')
    @patch('ringcentral_to_jobber.config.mock_mode', False)
    def test_find_customer_no_match(self, mock_post):
        #Test when there is no customer with matching results 
        self.client.access_token = "test_token"

        #Mock response with no customers 
        mock_response = Mock()
        mock_response.json.return_value = {
            'data': {
                'clients': {
                    'node': []
                }
            }
        }

        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        customer = self.client.find_customer_by_phone("+15551234567")
        self.assertIsNone(customer)

    @patch('ringcentral_to_jobber.config.mock_mode', True)
    def test_create_customer_note_mock(self):
        #Test in mock mode 
        result = self.client.create_customer_note("customer_123", "Test note")
        self.assertTrue(result)

    @patch('requests.post')
    @patch('ringcentral_to_jobber.config.mock_mode', False)
    def test_create_customer_note_mock(self, mock_post):
        self.client.access_token = "test_token"

        mock_response = Mock()
        mock_response.json.return_value = {
            'data': {
                'noteCreate': {
                    'note': {'id': 'note_123', 'body': 'Test note'},
                    'userErrors': []
                }
            }
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        result = self.client.create_customer_note("customer_123", "Test note")
        self.assertTrue(result)
    
    def test_token_storage(self):
        #test the token storage 
        token_data = {
            'access_token': 'new_token_123',
            'refresh_token': 'refresh_456',
            'expires_in': 3600
        }

        with patch('ringcentral_to_jobber.db_manager') as mock_db:
            self.client.store_token(token_data)

            self.assertEqual(self.client.access_token, 'new_token_123')
            mock_db.store_token.assert_called_once()

class TestRingCentralJobberSync(unittest.TestCase):
    #test main sync service 
    def setUp(self):
        with patch('ringcentral_to_jobber.db_manager'):
            with patch('ringcentral_to_jobber.RingCentralClient'), \
                patch('ringcentral_to_jobber.JobberClient'):
                    self.sync_service = RingCentralJobberSync()

    def test_format_call_note(self):
        phone = "+15551234567"
        start_time = "2025-01-12T14:30:00Z"
        duration = 185
        recording_url = "https://example.com/recording"

        note = self.sync_service._format_call_note(phone, start_time, duration, recording_url)

        self.assertIn(phone, note)
        self.assertIn("3m 5s", note)
        self.assertIn(recording_url, note)
        self.assertIn("Call Recording", note)

    def test_format_call_note_short_duration(self):

        note = self.sync_service._format_call_note("+15551234567", "2025-01-15T14:30:00Z", 45, "http://example.com")
        self.assertIn("45s", note)
        self.assertNotIn("0m", note)

   # @patch.object(RingCentralClient, 'get_recent_calls')
    #@patch.object(JobberClient, 'find_customer_by_phone')
    #@patch.object(JobberClient, 'create_customer_note')
    @patch('ringcentral_to_jobber.db_manager')
    def test_process_call_success(self, mock_db):
        #Test successful call processing 
        #Set the mocks up 
        mock_db.is_call_processed.return_value = False

        #mock_find_customer.return_value = {'id': 'customer_123', 'name': 'Test Customer'}
        #mock_create_note.return_value = True
        self.sync_service.jobber.create_customer_note.return_value = True
        self.sync_service.ringcentral.get_recording_url.return_value = "https://example.com/recording"

        call_data = {
            'id': 'call_123',
            'from': {'phoneNumber': '+15551234567'},
            'recording': {'contentUri': '/recording/123'},
            'startTime': '2025-01-15T14:30:00Z',
            'duration': 180
        }

        result = self.sync_service._process_call(call_data)

        self.assertTrue(result)
        self.sync_service.jobber.find_customer_by_phone.assert_called_once_with('+15551234567')
        self.sync_service.jobber.create_customer_note.assert_called_once()
        self.sync_service.ringcentral.get_recording_url.assert_called_once_with('/recording/123')
        #mock_find_customer.assert_called_once_with('+15551234567')
        #mock_create_note.assert_called_once()
        mock_db.mark_call_processed.assert_called_once()

    #@patch.object(RingCentralClient, 'get_recent_calls')
    @patch('ringcentral_to_jobber.db_manager')
    def test_process_call_already_processed(self, mock_db):
        #Test to make sure we skip already processed calls 
        mock_db.is_call_processed.return_value = True

        call_data = {'id': 'call_123'}
        result = self.sync_service._process_call(call_data)

        self.assertFalse(result)

    #@patch.object(RingCentralClient, 'get_recent_calls')
    @patch('ringcentral_to_jobber.db_manager')
    def test_process_call_no_recording(self, mock_db):
        "Test skipping calls without recordings"

        mock_db.is_call_processed.return_value = False
        call_data = {'id': 'call_123', 'from': {'phoneNumber': '+15551234567'}}
        result = self.sync_service._process_call(call_data)

        self.assertFalse(result)
    
    @patch('ringcentral_to_jobber.db_manager')
    def test_process_call_no_phone_number(self, mock_db):
        #Skipping calls without phone numbers

        mock_db.is_call_processed.return_value = False
        call_data = {
            'id': 'call_123',
            'recording': {'contentUri': '/recording/123'}

        }
        result = self.sync_service._process_call(call_data)
        self.assertFalse(result)
    @patch('ringcentral_to_jobber.db_manager')
    def test_process_call_customer_not_found(self, mock_db):
        mock_db.is_call_processed.return_value = False
        self.sync_service.jobber.find_customer_by_phone.return_value = None

        call_data = {
            'id': 'call_123',
            'from': {'phoneNumber': '+15551234567'},
            'recording': {'contentUri': '/recording/123'},
            'startTime': '2025-1-15T14:30:00Z',
            'duration': 180
        }

        result = self.sync_service._process_call(call_data)

        self.assertFalse(result)
        self.sync_service.jobber.find_customer_by_phone.assert_called_once_with('+15551234567')


class TestIntegration(unittest.TestCase):
    #Integration with the complete workflow 
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.originial_config = ringcentral_to_jobber.config
        self.originial_db_manager = ringcentral_to_jobber.db_manager

        #Create test config
        test_config = Config()
        test_config.mock_mode = True
        test_config.data_dir = self.temp_dir

        ringcentral_to_jobber.config = test_config 

        test_db_path = os.path.join(self.temp_dir, 'test.db')
        ringcentral_to_jobber.db_manager = DatabaseManager(test_db_path)

    def tearDown(self):
        ringcentral_to_jobber.config = self.originial_config
        ringcentral_to_jobber.db_manager = self.originial_db_manager
        shutil.rmtree(self.temp_dir)
    
    def test_end_to_end_sync_mock_mode(self):
        with patch('ringcentral_to_jobber.RingCentralClient'), \
            patch('ringcentral_to_jobber.JobberClient'):
        #Test complete sync workflow in mock mode
            sync_service = RingCentralJobberSync()

            with patch.object(sync_service, 'sync_calls') as mock_sync:
                sync_service.sync_calls()
                mock_sync.assert_called_once()
    
    @patch('ringcentral_to_jobber.requests.post')
    def test_authenticate_real_mode(self, mock_post):
        #Test authenticate in real mode but mocked HTTP calls

        original_mock_mode = ringcentral_to_jobber.config.mock_mode
        ringcentral_to_jobber.config.mock_mode = False

        try:
            #mock a successful auth response
            mock_response = Mock()
            mock_response.json.return_value = {
                'access_token': 'test_token_123',
                'expires_in': 3600
            }    

            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            #Mock config values needed for auth
            ringcentral_to_jobber.config.ringcentral_jwt = "test_jwt"
            ringcentral_to_jobber.config.ringcentral_client_id = "test_client_id"
            ringcentral_to_jobber.config.ringcentral_client_secret = "test_client_secret"

            client = RingCentralClient()
            result = client.authenticate()

            self.assertTrue(result)
            self.assertEqual(client.access_token, "test_token_123")

            stored_token = ringcentral_to_jobber.db_manager.get_token('ringcentral')
            self.assertIsNotNone(stored_token)
            self.assertEqual(stored_token['access_token'], 'test_token_123')

        finally:
            #restore mock mode
            ringcentral_to_jobber.config.mock_mode = original_mock_mode

    def test_authenticate_mock_mode(self):
        client = RingCentralClient()
        result = client.authenticate()

        self.assertTrue(result)
        self.assertEqual(client.access_token, 'mock_access_token')

        #in mock mode there should be no token stored in database
        stored_token = ringcentral_to_jobber.db_manager.get_token('ringcentral')
        self.assertIsNone(stored_token)
    
    def test_data_base_operations(self):

        token_data = {
            'access_token': 'test_token',
            'refresh_token': 'test_refresh',
            'expires_at': '2025-12-31T23:59:59'
        }

        ringcentral_to_jobber.db_manager.store_token('test_service', token_data)
        retrieved_token = ringcentral_to_jobber.db_manager.get_token('test_service')

        self.assertIsNotNone(retrieved_token)
        self.assertEqual(retrieved_token['access_token'], 'test_token')

        #test call processing tracking 
        call_data = {
            'id': 'test_call_123',
            'phone_number': '+15551234567',
            'customer_id': 'customer_456',
            'recording_url': 'https://example.com/recording',
            'duration': 180,
            'start_time': '2025-01-15T14:30:00Z'
        }

        #should not be processed initially 
        self.assertFalse(ringcentral_to_jobber.db_manager.is_call_processed('test_call_123'))

        #mark as processed
        ringcentral_to_jobber.db_manager.mark_call_processed(call_data)

        #now should be processed 
        self.assertTrue(ringcentral_to_jobber.db_manager.is_call_processed('test_call_123'))

        #test sync logging
        ringcentral_to_jobber.db_manager.log_sync_result(5, 1, 'completed_with_errors')




class TestUtilities(unittest.TestCase):
    #Test utility functions 

    def test_setup_logging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_data_dir = ringcentral_to_jobber.config.data_dir
            ringcentral_to_jobber.config.data_dir = temp_dir

            logger = setup_logging()

            self.assertIsNotNone(logger)

            self.assertTrue(os.path.exists(os.path.join(temp_dir, 'logs')))

            ringcentral_to_jobber.config.data_dir = old_data_dir

class TestIncomingCall(unittest.TestCase):
    def test_incoming_call_creation(self):
        call_time = datetime.now(timezone.utc)
        customer_info = {'id': 'customer_123', 'name': 'Test Customer'}

        call = IncomingCall(
            call_id='call_123',
            from_number='+15551234567',
            to_number='+15559876543',
            caller_name='Test Customer',
            start_time=call_time,
            status='incoming',
            customer_info=customer_info
        )
        self.assertEqual(call.call_id, 'call_123')
        self.assertEqual(call.from_number, '+15551234567')
        self.assertEqual(call.to_number, '+15559876543')
        self.assertEqual(call.caller_name, 'Test Customer')
        self.assertEqual(call.status, 'incoming')
        self.assertEqual(call.customer_info, customer_info)
    
    def test_incoming_call_to_dict(self):
        #Makes sure the incoming call converts to a dictionary 
        call_time = datetime.now(timezone.utc)
        call = IncomingCall(
            call_id='call_123',
            from_number='+15551234567',
            to_number='+15559876543',
            caller_name='Test Customer',
            start_time=call_time,
            status='incoming'
        )

        call_dict = call.to_dict()
        
        self.assertIsInstance(call_dict, dict)
        self.assertEqual(call_dict['call_id'], 'call_123')
        self.assertEqual(call_dict['start_time'], call_time.isoformat())
        self.assertIn('from_number', call_dict)
        self.assertIn('to_number', call_dict)

class TestCallNotificationManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test_db')

        with patch('ringcentral_to_jobber.db_manager'):
            from ringcentral_to_jobber import JobberClient, CallNotificationManager
            self.mock_jobber = Mock(spec=JobberClient)
            self.notification_manager = CallNotificationManager(self.mock_jobber)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)
    
    @patch('ringcentral_to_jobber.config.mock_mode', True)
    def test_setup_ringcentral_webhooks_mock_mode(self):
        #test the webhook setup
        mock_ringcentral = Mock()
        result = self.notification_manager.setup_ringcentral_webhooks(mock_ringcentral)

        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 'mock_webhook_123')
    
    @patch('ringcentral_to_jobber.config')
    @patch('requests.post')
    def test_setup_ringcentral_webhooks_success(self, mock_post, mock_config):
        #Test webhooks w/o mock mode
        mock_config.mock_mode = False
        mock_config.webhook_base_url = 'https://test.ngrok.io'
        mock_config.webhook_validation_token = 'test-token-123'

        mock_ringcentral = Mock()
        mock_ringcentral.access_token = 'test_token'
        mock_ringcentral.base_url = 'https://platform.ringcentral.com'

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'id': 'webhook_123'}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        result = self.notification_manager.setup_ringcentral_webhooks(mock_ringcentral)

        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 'webhook_123')
        self.assertEqual(self.notification_manager.webhook_subscription_id, 'webhook_123')

        mock_post.assert_called_once()

        call_args = mock_post.call_args
        self.assertIn('headers', call_args.kwargs)
        self.assertIn('json', call_args.kwargs)
        self.assertIn('timeout', call_args.kwargs)

        headers = call_args.kwargs['headers']
        self.assertIn('Authorization', headers)
        self.assertIn('Content-Type', headers)
        self.assertEqual(headers['Authorization'], 'Bearer test_token')

        webhook_data = call_args.kwargs['json']
        self.assertIn('eventFilters', webhook_data)
        self.assertIn('deliveryMode', webhook_data)
        self.assertIn('expiresIn', webhook_data)

        delivery_mode = webhook_data['deliveryMode']
        self.assertEqual(delivery_mode['transportType'], 'WebHook')
        self.assertIn('test.ngrok.io', delivery_mode['address'])
        self.assertEqual(delivery_mode['validationToken'], 'test-token-123')

    @patch('ringcentral_to_jobber.config')
    @patch('requests.post')
    def test_setup_ringcentral_webhooks_failure(self, mock_post, mock_config):
        mock_config.mock_mode = False
        mock_config.webhook_base_url = 'htt[s://test.ngrok.io'
        mock_config.webhook_validation_token = 'test_token-123'

        mock_ringcentral = Mock()
        mock_ringcentral.access_token = 'test_token'
        mock_ringcentral.base_url = 'https://platform.ringcentral.com'

        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.test = 'Bad Request'
        mock_post.return_value = mock_response

        result = self.notification_manager.setup_ringcentral_webhooks(mock_ringcentral)

        self.assertIsNone(result)
    
    @patch('ringcentral_to_jobber.config')
    @patch('requests.post')
    def test_setup_ringcentral_webhooks_exception(self, mock_post, mock_config):
        """Test webhook setup with exception"""
        mock_config.mock_mode = False
        mock_config.webhook_base_url = 'https://test.ngrok.io'
        mock_config.webhook_validation_token = 'test-token-123'
        
        mock_ringcentral = Mock()
        mock_ringcentral.access_token = 'test_token'
        mock_ringcentral.base_url = 'https://platform.ringcentral.com'
        
        # Mock an exception during POST
        mock_post.side_effect = Exception('Network error')
        
        result = self.notification_manager.setup_ringcentral_webhooks(mock_ringcentral)
        
        self.assertIsNone(result)

    @patch('ringcentral_to_jobber.db_manager')
    def test_handle_incoming_call(self, mock_db):
        #Test to see if calls are handled properly
        session_data = {
            'id': 'session_123',
            'from': {'phoneNumber': '+15551234567'},
            'to': {'phoneNumber': '+15559876543'},
            'status': 'Proceeding',
            'direction': 'Inbound'

        }
        self.mock_jobber.find_customer_by_phone.return_value = {
            'id': 'customer_123',
            'name': 'Test Customer'
        }

        with patch.object(self.notification_manager, 'lookup_customer_in_jobber') as mock_lookup:
            mock_lookup.return_value = {
                'id': 'customer_123',
                'name': 'Test Customer',
                'phone': '+15551234567'
            }

            self.notification_manager._handle_incoming_call(session_data)

            self.assertIn('session_123', self.notification_manager.active_calls)
            call = self.notification_manager.active_calls['session_123']
            self.assertEqual(call.from_number, '+15551234567')
            self.assertEqual(call.caller_name, 'Test Customer')
            mock_db.store_call_notifications.assert_called_once()
    
    def test_handle_call_ended(self):
        call_time = datetime.now(timezone.utc)
        test_call = IncomingCall(
            call_id='session_123',
            from_number='+15551234567',
            to_number='+15559876543',
            caller_name='Test Customer',
            start_time=call_time,
            status='incoming'
        )

        self.notification_manager.active_calls['session_123'] = test_call 
        self.notification_manager._handle_call_ended('session_123')

        self.assertEqual(self.notification_manager.active_calls['session_123'].status, 'ended')

        self.assertIn('session_123', self.notification_manager.active_calls)


    @patch('threading.Timer')
    def test_handle_call_ended_with_timer_mock(self, mock_timer):
        #Handling call ended with a mock timer 
        mock_timer_instance = Mock()
        mock_timer.return_value = mock_timer_instance

        call_time = datetime.now(timezone.utc)
        test_call = IncomingCall(
            call_id='session_123',
            from_number='+15551234567',
            to_number='+15559876543',
            caller_name='Test Customer',
            start_time=call_time,
            status='incoming'
        )
        self.notification_manager.active_calls['session_123'] = test_call
        
        #end the call
        self.notification_manager._handle_call_ended('session_123')

        #Make sure the status is updates 
        self.assertEqual(self.notification_manager.active_calls['session_123'].status, 'ended')

        #Verifys timer was created and started 
        mock_timer.assert_called_once_with(30.0, unittest.mock.ANY)
        mock_timer_instance.start.assert_called_once()

        #veryify the daemon was set 
        self.assertTrue(mock_timer_instance.daemon)
    
    def test_handle_call_ended_nonexistent_call(self):
        self.notification_manager._handle_call_ended('nonexistent_call_id')
        
        # Should not affect active calls
        self.assertEqual(len(self.notification_manager.active_calls), 0)

    def test_handle_call_ended_delayed_removal(self):
        call_time = datetime.now(timezone.utc)
        test_call = IncomingCall(
            call_id='session_123',
            from_number='+15551234567',
            to_number='+15559876543',
            caller_name='Test Customer',
            start_time=call_time,
            status='incoming'
        )

        original_method = self.notification_manager._handle_call_ended

        def quick_handle_call_ended(call_id):
            if call_id in self.notification_manager.active_calls:
                call = self.notification_manager.active_calls[call_id]
                call.status = 'ended'

                timer = threading.Timer(0.1, lambda: self.notification_manager.active_calls.pop(call_id, None))
                timer.daemon = True
                timer.start()
        self.notification_manager._handle_call_ended = quick_handle_call_ended
        self.notification_manager.active_calls['session_123'] = test_call

        #end call
        self.notification_manager._handle_call_ended('session_123')

        #call should still be in the active calls 
        self.assertIn('session_123', self.notification_manager.active_calls)
        self.assertEqual(self.notification_manager.active_calls['session_123'].status, 'ended')

        time.sleep(0.2)
        # Should be removed now
        self.assertNotIn('session_123', self.notification_manager.active_calls)
        
        # Restore original method
        self.notification_manager._handle_call_ended = original_method
    
    @patch('ringcentral_to_jobber.config.mock_mode', True)
    def test_lookup_customer_in_jobber_mock(self):
        result = self.notification_manager.lookup_customer_in_jobber('+15551234567')
        
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 'mock_customer_123')
        self.assertEqual(result['name'], 'Test Customer')
        self.assertIn('jobber_uri', result)
    
    @patch('ringcentral_to_jobber.config.mock_mode', False)
    def test_lookup_customer_in_jobber_real(self):
        """Test customer lookup in real mode"""
        self.mock_jobber.find_customer_by_phone.return_value = {
            'id': 'customer_123',
            'name': 'John Doe'
        }
        
        result = self.notification_manager.lookup_customer_in_jobber('+15551234567')
        
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 'customer_123')
        self.assertEqual(result['name'], 'John Doe')
        self.mock_jobber.find_customer_by_phone.assert_called_once_with('+15551234567')
    
    def test_process_call_events(self):
        """Test processing call events"""
        event_data = {
            'body': {
                'telephonySessionsEvent': {
                    'telephonySessions': [
                        {
                            'id': 'session_123',
                            'status': 'Proceeding',
                            'direction': 'Inbound',
                            'from': {'phoneNumber': '+15551234567'},
                            'to': {'phoneNumber': '+15559876543'}
                        }
                    ]
                }
            }
        }
        
        with patch.object(self.notification_manager, '_handle_telephony_session') as mock_handle:
            self.notification_manager.process_call_events(event_data)
            mock_handle.assert_called_once()
    @patch('ringcentral_to_jobber.db_manager')
    def test_handle_telephony_session_incoming(self, mock_db):
        """Test handling telephony session for incoming calls"""
        session_data = {
            'id': 'session_123',
            'status': 'Proceeding',
            'direction': 'Inbound',
            'from': {'phoneNumber': '+15551234567'},
            'to': {'phoneNumber': '+15559876543'}
        }
        
        with patch.object(self.notification_manager, 'lookup_customer_in_jobber') as mock_lookup:
            mock_lookup.return_value = {
                'id': 'customer_123',
                'name': 'Test Customer',
                'phone': '+15551234567'
            }
            self.notification_manager._handle_telephony_session(session_data)

            #Verify call was added to active calls 
            self.assertIn('session_123', self.notification_manager.active_calls)
            call = self.notification_manager.active_calls['session_123']
            self.assertEqual(call.from_number, '+15551234567')
            self.assertEqual(call.caller_name, 'Test Customer')
            self.assertEqual(call.status, 'incoming')

            mock_lookup.assert_called_once_with('+15551234567')

            #makesure it was stored in db
            mock_db.store_call_notifications.assert_called_once()

    def test_handle_telephony_session_ended(self):
        session_data_ended = {
            'id': 'session_123',
            'status': 'Disconnected'
        }
        #adds an call thats going to end 
        call_time = datetime.now(timezone.utc)
        
        test_call = IncomingCall(
            call_id='session_123',
            from_number='+15551234567',
            to_number='+15559876543',
            caller_name='Test Customer',
            start_time=call_time,
            status='incoming'
        )
        self.notification_manager.active_calls['session_123'] = test_call

        #Handles the session ending
        with patch('threading.Timer') as mock_timer:
            mock_timer_instance = Mock()
            mock_timer.return_value = mock_timer_instance
            self.notification_manager._handle_telephony_session(session_data_ended)

            self.assertEqual(self.notification_manager.active_calls['session_123'].status, 'ended')
            mock_timer.assert_called_once()

    def test_handle_telephony_session_other_status(self):
        #Handles with other statuses 
        session_data = {
            'id': 'session_123',
            'status': 'Ringing',  # Not Proceeding or Disconnected/Finished
            'direction': 'Inbound'
            }
        initial_count = len(self.notification_manager.active_calls)
        self.notification_manager._handle_telephony_session(session_data)

        #no change should happen
        self.assertEqual(len(self.notification_manager.active_calls), initial_count)
    
    def test_handle_telephony_session_outbound(self):
        session_data = {
            'id': 'session_123',
            'status': 'Proceeding',
            'direction': 'Outbound',  # Not Inbound
            'from': {'phoneNumber': '+15551234567'},
            'to': {'phoneNumber': '+15559876543'}
        }
        inital_count = len(self.notification_manager.active_calls)
        self.notification_manager._handle_telephony_session(session_data)

        self.assertEqual(len(self.notification_manager.active_calls), inital_count)

    @patch('ringcentral_to_jobber.db_manager')
    def test_handle_incoming_call_no_customer(self, mock_db):
        session_data = {
            'id': 'session_456',
            'from': {'phoneNumber': '+15559999999'},
            'to': {'phoneNumber': '+15559876543'}
        }

        #Mock customer lookup returning none 
        with patch.object(self.notification_manager, 'lookup_customer_in_jobber') as mock_lookup:
            mock_lookup.return_value = None
            
            self.notification_manager._handle_incoming_call(session_data)
            #make sure call was added with unknown caller 
            self.assertIn('session_456', self.notification_manager.active_calls)
            call = self.notification_manager.active_calls['session_456']
            self.assertEqual(call.from_number, '+15559999999')
            self.assertEqual(call.caller_name, 'Unknown Caller')
            self.assertIsNone(call.customer_info)

            #verify db storage was called with None customer Id
            call_args = mock_db.store_call_notifications.call_args[0][0]
            self.assertIsNone(call_args['customer_id'])

class TestNotificationDashboardApp(unittest.TestCase):
    #Testing the flask dashboard app

    def setUp(self):
        self.mock_call_manager = Mock(spec=CallNotificationManager)
        self.mock_sync_service = Mock()
        self.mock_call_manager.process_call_event = Mock(return_value=None)
        self.app = NotificationDashboardApp(self.mock_call_manager, self.mock_sync_service)
        self.client = self.app.app.test_client()
        self.app.app.config['TESTING'] = True

    def test_dashboard_route(self):
        #Test HTML route
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'RingCentral Call Notifications', response.data)
        self.assertIn(b'Real-time incoming call monitoring', response.data)
    
    def test_get_active_calls_route(self):
        #tests the active calls api route 
        mock_call = Mock()
        mock_call.to_dict.return_value = {
            'call_id': 'call_123',
            'from_number': '+15551234567',
            'caller_name': 'Test Customer',
            'status': 'incoming'

        }
        self.mock_call_manager.active_calls = {'call_123': mock_call}
        response  = self.client.get('/api/calls/active')

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn('calls', data)
        self.assertEqual(len(data['calls']), 1)
        self.assertEqual(data['calls'][0]['call_id'], 'call_123')
    
    def test_get_customer_jobber_link_success(self):
        mock_call = Mock()
        mock_call.customer_info = {
            'id': 'customer_123',
            'name': 'Test Customer',
            'jobber_uri': 'https://test.getjobber.com/clients/customer_123'
        }
        self.mock_call_manager.active_calls = {'call_123': mock_call}

        response = self.client.get('/api/calls/call_123/customer')

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn('customer', data)
        self.assertIn('jobber_link', data)
    
    def test_get_customer_in_jobber_link_not_found(self):
        self.mock_call_manager.active_calls = {}
        response = self.client.get('/api/calls/call_123/customer')
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertIn('error', data)
    
    def test_create_call_note_success(self):
        mock_call = Mock()
        mock_call.customer_info = {'id': 'customer_123'}
        mock_call.start_time = datetime.now(timezone.utc)
        mock_call.from_number = '+15551234567'
        mock_call.caller_name = 'Test Customer'
        mock_call.status = 'incoming'

        self.mock_call_manager.active_calls = {'call_123': mock_call}
        self.mock_sync_service.jobber.create_customer_note.return_value = True

        response = self.client.post('/api/calls/call_123/create-note', json ={'note': 'Test note content'})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['message'], 'Note created successfully')

        self.mock_sync_service.jobber.create_customer_note.assert_called_once()

        #verify note content was passed 
        call_args = self.mock_sync_service.jobber.create_customer_note.call_args
        customer_id = call_args[0][0]
        note_content = call_args[0][1]

        self.assertEqual(customer_id, 'customer_123')
        self.assertIn('Test note content', note_content)
        self.assertIn('Test Customer', note_content)
        self.assertIn('+15551234567', note_content)
    
    def test_create_call_note_failure(self):
        mock_call = Mock()
        mock_call.customer_info = {'id': 'customer_123'}
        mock_call.start_time = datetime.now(timezone.utc)
        mock_call.from_number = '+15551234567'
        mock_call.caller_name = 'Test Customer'
        mock_call.status = 'incoming'

        self.mock_call_manager.active_calls = {'call_123': mock_call}
        self.mock_sync_service.jobber.create_customer_note.return_value = False

        response = self.client.post('/api/calls/call_123/create-note', json={'note': 'Test note content'})

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertIn('error', data)
    
    def test_create_call_note_exception(self):
        mock_call = Mock()
        mock_call.customer_info = {'id': 'customer_123'}
        mock_call.start_time = datetime.now(timezone.utc)
        mock_call.from_number = '+15551234567'
        mock_call.caller_name = 'Test Customer'
        mock_call.status = 'incoming'

        self.mock_call_manager.active_calls = {'call_123': mock_call}
        self.mock_sync_service.jobber.create_customer_note.side_effect = Exception('API Error')

        response = self.client.post('/api/calls/call_123/create-note', json={'note': 'Test note content'})

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertEqual(data['error'], 'Failed to create note')
    
    def test_create_call_note_no_json_data(self):
        mock_call = Mock()
        mock_call.customer_info = {'id': 'customer_123'}
        mock_call.start_time = datetime.now(timezone.utc)
        mock_call.from_number = '+15551234567'
        mock_call.caller_name = 'Test Customer'
        mock_call.status = 'incoming'

        self.mock_call_manager.active_calls = {'call_123': mock_call}
        self.mock_sync_service.jobber.create_customer_note.return_value = True

        response = self.client.post('/api/calls/call_123/create-note')

        #Should still work with no json value 
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])

        call_args = self.mock_sync_service.jobber.create_customer_note.call_args
        note_content = call_args[0][1]
        self.assertIn('No additional notes', note_content)
    
    def test_get_sync_status_route(self):
        self.mock_sync_service.is_running = True
        self.mock_call_manager.webhook_subscription_id = 'webhook_123'
        self.mock_call_manager.active_calls = {'call_123': Mock()}

        response = self.client.get('/api/sync/status')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['sync_running'])
        self.assertTrue(data['webhook_active'])
        self.assertEqual(data['active_calls'], 1)
    
    @patch('ringcentral_to_jobber.config')
    def test_hadnle_ringcentral_webhook_validation(self, mock_config):
        mock_config.webhook_validation_token = 'test-token-123'

        response = self.client.post('/ringcentral/webhook', 
                                    headers={'Validation-Token': 'test-token-123'})
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('Validation-Token'), 'test-token-123')

    @patch('ringcentral_to_jobber.config')
    def test_handle_ringcentral_webhook_invalid_token(self, mock_config):
        #Test webhook with invalid validation token
        mock_config.webhook_validation_token = 'test-token-123'
        response = self.client.post('/ringcentral/webhook',
                                    headers={'Validation-token': 'wrong-token'})
        self.assertEqual(response.status_code, 401)
    
    @patch('ringcentral_to_jobber.config')
    def test_handle_ringcentral_webhook_event_processing(self, mock_config):
        mock_config.webhook_validation_token = 'test-token-123'
        event_data = {
            'body': {
                'telephonySessionsEvent': {
                    'telephonySessions': [
                        {
                    'id': 'session_123',
                    'status': 'Proceeding',
                    'direction': 'Inbound'
                        }
                    ]
                }
            }
        }
        self.mock_call_manager.process_call_event.reset_mock()
        response = self.client.post('/ringcentral/webhook',
                                    json=event_data,
                                    headers={'Validation-Token': 'test-token-123'})
        if response.status_code != 200:
            print(f"Response status: {response.status_code}")
            print(f"Response data: {response.data}")
            try:
                error_data = json.loads(response.data)
                print(f"Error details: {error_data}")
            except:
                pass

        self.assertEqual(response.status_code, 200)
        self.mock_call_manager.process_call_event.assert_called_once_with(event_data)

    @patch('ringcentral_to_jobber.config')
    def test_handle_ringcentral_webhook_validation(self, mock_config):
        """Test webhook validation response"""
        mock_config.webhook_validation_token = 'test-token-123'
    
    # Test the validation response (what RingCentral sends during webhook setup)
        response = self.client.post('/ringcentral/webhook',
                              headers={'Validation-Token': 'test-token-123'})
    
        self.assertEqual(response.status_code, 200)
    
    # Should return validation token
        data = json.loads(response.data)
        self.assertEqual(data['validationToken'], 'test-token-123')
    
        #Should set response header
        self.assertEqual(response.headers.get('Validation-Token'), 'test-token-123')

    @patch('ringcentral_to_jobber.config')
    def test_handle_ringcentral_webhook_invalid_token(self, mock_config):
        mock_config.webhook_validation_token = 'test-token-123'
    
        event_data = {
            'body': {
            'telephonySessionsEvent': {
                'telephonySessions': [
                    {
                        'id': 'session_123',
                        'status': 'Proceeding',
                        'direction': 'Inbound'
                        }
                    ]
                }
            }
        }
    
        # Send with wrong validation token
        response = self.client.post('/ringcentral/webhook',
                              json=event_data,
                              headers={'Validation-Token': 'wrong-token'})
    
        self.assertEqual(response.status_code, 401)
        data = json.loads(response.data)
        self.assertEqual(data['error'], 'Invalid validation token')


class TestDatabaseEnhancements(unittest.TestCase):
    #Tests db functionality 
    def setUp(self):
        #Sets up the db 
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test_db')
        self.db_manager = DatabaseManager(self.db_path)
    
    def tearDown(self) -> None:
        if hasattr(self.db_manager, 'conn'):
            self.db_manager.conn.close()
        shutil.rmtree(self.temp_dir)
    
    def test_store_call_notification_success(self):
        call_data = {
            'call_id': 'call_123',
            'from_number': '+15551234567',
            'to_number': '+15559876543',
            'caller_name': 'Test Customer',
            'start_time': datetime.now(timezone.utc),
            'status': 'incoming',
            'customer_id': 'customer_123'
        }

        self.db_manager.store_call_notifications(call_data)
        #verify the record was stored
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM call_notifications WHERE call_id = ?", ('call_123',))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            if row:
            # Assuming the table structure matches your INSERT statement
                self.assertEqual(row[0], 'call_123')  # call_id
                self.assertEqual(row[1], '+15551234567')  # from_number
                self.assertEqual(row[2], '+15559876543')  # to_number
                self.assertEqual(row[3], 'Test Customer')  # caller_name
                self.assertEqual(row[5], 'incoming')  # status
                self.assertEqual(row[6], 'customer_123')
    
    def test_store_call_notification_error_handling(self):
        invalid_call_data = {
            'call_id': None,
            'from_number': '+15551234567'
        }


        self.db_manager.store_call_notifications(invalid_call_data)

    def test_database_table_creation(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                                  SELECT name FROM sqlite_master
                                  WHERE type='table' AND name='call_notifications'
                                  """)
            result = cursor.fetchone()
            self.assertIsNotNone(result)
            

        


    




if __name__ == '__main__':
    '''
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics('lineno')
    print("[Top 10]")
    for stat in top_stats:
        print(stat)
'''
    unittest.main(verbosity=2)
    
        










