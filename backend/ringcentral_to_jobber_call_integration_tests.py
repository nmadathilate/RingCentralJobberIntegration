import unittest
from unittest.mock import patch, Mock

class TestCallNotificationManagerWithNgrok(unittest.TestCase):
    """Test webhook setup with correct ngrok URL format"""
    
    def setUp(self):
        """Set up test environment"""
        with patch('ringcentral_to_jobber.db_manager'):
            self.mock_jobber = Mock()
            from ringcentral_to_jobber import CallNotificationManager
            self.notification_manager = CallNotificationManager(self.mock_jobber)
    
    @patch('ringcentral_to_jobber.config')
    @patch('requests.post')
    def test_setup_ringcentral_webhooks_with_ngrok_url(self, mock_post, mock_config):
        """Test webhook setup with proper ngrok URL format"""
        # Use the format from your ngrok screenshots
        mock_config.mock_mode = False
        mock_config.webhook_base_url = 'https://more-louse-amazingly.ngrok-free.app'
        mock_config.webhook_validation_token = 'your-secure-random-token-123'
        
        # Set up RingCentral client mock
        mock_ringcentral = Mock()
        mock_ringcentral.access_token = 'test_token'
        mock_ringcentral.base_url = 'https://platform.ringcentral.com'
        
        # Set up successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'id': 'webhook_123',
            'uri': 'https://platform.ringcentral.com/restapi/v1.0/subscription/webhook_123',
            'eventFilters': [
                '/restapi/v1.0/account/~extenstion/~/telephony/sessions',
                '/restapi/v1.0/account/~extension/~/presence?detailedTelephonyState=true'
            ]
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        # Execute the test
        result = self.notification_manager.setup_ringcentral_webhooks(mock_ringcentral)
        
        # Verify results
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 'webhook_123')
        self.assertEqual(self.notification_manager.webhook_subscription_id, 'webhook_123')
        
        # Verify the POST request was made correctly
        mock_post.assert_called_once()
        
        # Check the webhook data that was sent
        call_args = mock_post.call_args
        webhook_data = call_args.kwargs['json']
        
        # Verify webhook address is correct
        expected_webhook_url = 'https://more-louse-amazingly.ngrok-free.app/ringcentral/webhook'
        self.assertEqual(webhook_data['deliveryMode']['address'], expected_webhook_url)
        
        # Verify event filters are set correctly
        self.assertIn('eventFilters', webhook_data)
        self.assertEqual(len(webhook_data['eventFilters']), 2)
        
        # Verify validation token
        self.assertEqual(webhook_data['deliveryMode']['validationToken'], 'your-secure-random-token-123')
    
    def test_ngrok_url_validation(self):
        """Test that ngrok URL format is valid"""
        test_urls = [
            'https://more-louse-amazingly.ngrok-free.app',
            'https://abc-def-ghi.ngrok-free.app',
            'https://test-domain.ngrok.io',  # Older format
        ]
        
        for url in test_urls:
            # Basic validation
            self.assertTrue(url.startswith('https://'))
            self.assertIn('ngrok', url)
            
            # Should not end with incomplete suffixes
            self.assertFalse(url.endswith('ngrok-f'))
            self.assertFalse(url.endswith('ngrok-'))

# Test to help you verify your actual setup
class TestYourActualNgrokSetup(unittest.TestCase):
    """Helper test to verify your actual ngrok configuration"""
    
    def test_current_env_config(self):
        """Test your current environment configuration"""
        import os
        
        webhook_url = os.getenv('WEBHOOK_BASE_URL', '')
        validation_token = os.getenv('WEBHOOK_VALIDATION_TOKEN', '')
        redirect_uri = os.getenv('JOBBER_REDIRECT_URI', '')
        
        print(f"Current webhook URL: {webhook_url}")
        print(f"Current redirect URI: {redirect_uri}")
        print(f"Validation token length: {len(validation_token)}")
        
        # Validation checks
        if webhook_url:
            self.assertTrue(webhook_url.startswith('https://'))
            if 'ngrok' in webhook_url:
                # Should be complete ngrok URL
                self.assertTrue(
                    webhook_url.endswith('.app') or webhook_url.endswith('.io'),
                    f"Incomplete ngrok URL: {webhook_url}"
                )
        
        # Redirect URI should match webhook domain for consistency
        if webhook_url and redirect_uri:
            webhook_domain = webhook_url.split('/')[2]  # Extract domain
            redirect_domain = redirect_uri.split('/')[2]
            
            if 'ngrok' in webhook_url:
                self.assertEqual(webhook_domain, redirect_domain,
                               "Webhook and redirect URIs should use the same ngrok domain")

if __name__ == '__main__':
    # Run the helper test first to check your configuration
    suite = unittest.TestLoader().loadTestsFromTestCase(TestYourActualNgrokSetup)
    runner = unittest.TextTestRunner(verbosity=2)
    print("=== Checking Your Current Configuration ===")
    runner.run(suite)
    
    print("\n=== Running Webhook Tests ===")
    # Then run the actual tests
    unittest.main(verbosity=2)