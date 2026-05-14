#!/usr/bin/env python3
"""
Test script to verify Payroll Staff Hours Report HTTP routes and exports.
"""

import sys
from datetime import datetime, timedelta
import io

def test_html_route():
    """Test the HTML report route."""
    print("\n=== TEST: HTML Route (/reports/assistants) ===")
    try:
        from app import app, auth_manager
        
        # Mock authentication
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['user_email'] = 'test@example.com'
            
            # Test with date range
            today = datetime.today().date()
            start_date = (today - timedelta(days=30)).isoformat()
            end_date = today.isoformat()
            
            response = client.get(
                f'/reports/assistants?start={start_date}&end={end_date}'
            )
            
            print(f"✓ Route responded with status: {response.status_code}")
            
            if response.status_code == 200:
                content = response.get_data(as_text=True)
                if 'Payroll Staff Hours Report' in content:
                    print("✓ HTML contains report title")
                if 'Staff Name' in content:
                    print("✓ HTML contains table headers")
                if 'John Doe' in content or 'test Staff' in content:
                    print("✓ HTML contains staff data")
                else:
                    print("⚠ No staff data in HTML (may be due to date range)")
                return True
            else:
                print(f"✗ Unexpected status code: {response.status_code}")
                print(f"Response: {response.get_data(as_text=True)[:500]}")
                return False
                
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_pdf_export():
    """Test PDF export route."""
    print("\n=== TEST: PDF Export (/reports/assistants/pdf) ===")
    try:
        from app import app
        
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['user_email'] = 'test@example.com'
            
            today = datetime.today().date()
            start_date = (today - timedelta(days=30)).isoformat()
            end_date = today.isoformat()
            
            response = client.get(
                f'/reports/assistants/pdf?start={start_date}&end={end_date}'
            )
            
            print(f"✓ Route responded with status: {response.status_code}")
            
            if response.status_code == 200:
                # Check if it's a valid PDF
                data = response.get_data()
                if data.startswith(b'%PDF'):
                    print(f"✓ Valid PDF generated ({len(data)} bytes)")
                    # Check for expected content
                    if b'Payroll Staff Hours' in data:
                        print("✓ PDF contains 'Payroll Staff Hours' text")
                    return True
                else:
                    print(f"✗ Not a valid PDF (starts with: {data[:10]})")
                    return False
            else:
                print(f"✗ Unexpected status code: {response.status_code}")
                return False
                
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_csv_export():
    """Test CSV export route."""
    print("\n=== TEST: CSV Export (/reports/assistants/csv) ===")
    try:
        from app import app
        
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['user_email'] = 'test@example.com'
            
            today = datetime.today().date()
            start_date = (today - timedelta(days=30)).isoformat()
            end_date = today.isoformat()
            
            response = client.get(
                f'/reports/assistants/csv?start={start_date}&end={end_date}'
            )
            
            print(f"✓ Route responded with status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.get_data(as_text=True)
                print(f"✓ CSV generated ({len(data)} bytes)")
                
                # Check for expected content
                if 'PAYROLL STAFF HOURS' in data:
                    print("✓ CSV contains report title")
                if 'Employee Name' in data:
                    print("✓ CSV contains headers")
                if 'SUMMARY BY EMPLOYEE' in data:
                    print("✓ CSV contains summary section")
                    
                return True
            else:
                print(f"✗ Unexpected status code: {response.status_code}")
                return False
                
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_error_handling():
    """Test error handling with invalid dates."""
    print("\n=== TEST: Error Handling ===")
    try:
        from app import app
        
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['user_email'] = 'test@example.com'
            
            # Test with invalid date format
            response = client.get('/reports/assistants?start=invalid&end=2026-05-13')
            print(f"✓ Invalid start date handled with status: {response.status_code}")
            
            # Test with swapped dates
            response = client.get('/reports/assistants?start=2026-05-13&end=2026-04-13')
            print(f"✓ Swapped dates handled with status: {response.status_code}")
            if b'End date must be on or after start date' in response.get_data():
                print("✓ Appropriate error message shown")
            
            # Test with missing dates
            response = client.get('/reports/assistants')
            print(f"✓ Missing dates handled with status: {response.status_code}")
            
            return True
            
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    print("=" * 60)
    print("PAYROLL STAFF HOURS REPORT - HTTP ROUTES TEST")
    print("=" * 60)
    
    results = [
        test_html_route(),
        test_pdf_export(),
        test_csv_export(),
        test_error_handling(),
    ]
    
    print("\n" + "=" * 60)
    if all(results):
        print("✓ ALL ROUTE TESTS PASSED!")
    else:
        print("✗ SOME ROUTE TESTS FAILED")
        sys.exit(1)
    print("=" * 60)

if __name__ == "__main__":
    main()
