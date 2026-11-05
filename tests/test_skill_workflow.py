"""Regression checks for the SKILL.md single-click workflow; no live booking."""
import time
import unittest
from unittest.mock import Mock, patch

import app


class SkillWorkflowTests(unittest.TestCase):
    def test_skill_target_subtracts_one_second_and_normalizes_pnr(self):
        controller = app.Controller()
        with patch.object(app, 'scheduled_timestamp', return_value=1900000000.125):
            controller.submit('schedule', {'pnr': 'ab12cd', 'departure': 'Singapore', 'timezone': 'Asia/Singapore'})
        _, payload = controller.commands.get_nowait()
        self.assertEqual(payload['target'], payload['checkin'] - 1)
        self.assertEqual(payload['booking']['pnr'], 'AB12CD')
        self.assertFalse(payload['request_mode'])

    def test_selects_departure_before_filling_pnr(self):
        form = app.BookingForm(Mock())
        pnr, departure, button = Mock(), Mock(), Mock()
        calls = Mock()
        calls.attach_mock(pnr.fill, 'fill')
        form.fields = Mock(return_value=(pnr, departure, button))
        form.select_departure = calls.select
        form.select_departure.return_value = 'Singapore'
        form.verify = Mock()
        form.prepare({'pnr': 'AB12CD', 'departure': 'Singapore'})
        self.assertEqual([call[0] for call in calls.mock_calls], ['select', 'fill'])

    def test_clicks_once_without_request_gate_and_keeps_session(self):
        controller = app.Controller()
        controller.page = Mock()
        due = time.time() + 1
        controller.update('ready', 'Armed', due)
        def response(_):
            controller.click_timing.update(first_response_status=200)
        controller.page.wait_for_timeout.side_effect = response
        with patch.object(app, 'wait_until', return_value=True), \
             patch.object(app.BookingForm, 'dispatch_click', return_value={'clicked': True, 'event_at': due}) as dispatch, \
             patch.object(app.BookingRequestGate, 'install') as gate, \
             patch.object(app.time, 'time', return_value=due):
            controller.click({'target': due, 'checkin': due + 1, 'booking': {'pnr': 'AB12CD'}, 'click_fields': {}})
        dispatch.assert_called_once()
        gate.assert_not_called()
        controller.page.close.assert_not_called()
        self.assertEqual(controller.snapshot()['status'], 'submitted')

    def test_missed_deadline_never_clicks(self):
        controller = app.Controller()
        controller.page = Mock()
        controller.update('ready', 'Armed', 100)
        with patch.object(app.BookingForm, 'dispatch_click') as dispatch:
            controller.click({'target': 100, 'booking': {'pnr': 'AB12CD'}, 'click_fields': {}})
        dispatch.assert_not_called()
        self.assertIn('missed', controller.snapshot()['message'])

    def test_timeout_preserves_browser_without_retry(self):
        controller = app.Controller()
        controller.page = Mock()
        due = time.time() + 1
        controller.update('ready', 'Armed', due)
        with patch.object(app, 'wait_until', return_value=True), \
             patch.object(app.BookingForm, 'dispatch_click', return_value={'clicked': True}) as dispatch, \
             patch.object(app.time, 'time', return_value=due), patch.object(app, 'RESULT_WATCH', 0):
            controller.click({'target': due, 'booking': {'pnr': 'AB12CD'}, 'click_fields': {}})
        dispatch.assert_called_once()
        self.assertEqual(controller.snapshot()['status'], 'uncertain')
        controller.page.close.assert_not_called()

    def test_countdown_shows_both_times_with_offset(self):
        text = app.stage_countdown({'status': 'ready', 'target': 1900000000.125,
                                   'now': 1800000000, 'timezone': 'Asia/Singapore', 'request_mode': False})
        self.assertIn('Check-in time:', text)
        self.assertIn('minus one second', text)
        self.assertIn('+08:00', text)

    def test_unrelated_api_traffic_does_not_end_retrieval(self):
        controller = app.Controller()
        controller.click_timing = {"target": 100}
        request = Mock(method="GET", url="https://example.test/telemetry")
        controller.request_observed(request)
        self.assertIsNone(controller.observed_request)
        request = Mock(method="POST", url="https://apigw.singaporeair.com/chopemyseat/booking/retrieval")
        controller.request_observed(request)
        self.assertIs(controller.observed_request, request)

    def test_one_second_subtraction_crosses_local_midnight(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        checkin = datetime(2027, 1, 2, 0, 0, 0, 125000, ZoneInfo("Asia/Singapore")).timestamp()
        controller = app.Controller()
        with patch.object(app, 'scheduled_timestamp', return_value=checkin):
            controller.submit('schedule', {'pnr': 'ab12cd', 'departure': 'Singapore', 'timezone': 'Asia/Singapore'})
        _, payload = controller.commands.get_nowait()
        self.assertEqual(app.display_time(payload['target'], 'Asia/Singapore'), '2027-01-01 23:59:59.125+08:00')
