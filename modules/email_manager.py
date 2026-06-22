"""
Email Manager Module for Stdytime
Handles email sending functionality for reports, notifications, and student communications
Version: 06.00.00
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import json
import os
from typing import Optional, List, Dict


_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_CONFIG_PATH = os.path.join(_APP_ROOT, 'db_config.json')


BRAND_PRIMARY = '#2e7d32'
BRAND_PRIMARY_DARK = '#1b5e20'
BRAND_ACCENT = '#fdd835'
BRAND_SURFACE = '#f6faf4'
BRAND_SURFACE_STRONG = '#e8f5e9'
BRAND_BORDER = '#d7e7d5'
BRAND_TEXT = '#1f2937'
BRAND_MUTED = '#667085'


def resolve_center_name(center_name: Optional[str] = None) -> str:
    """Resolve the center name from the provided value or instructor profile."""
    if center_name and str(center_name).strip():
        return str(center_name).strip()

    # Prefer an explicit app-level center name if the launcher/config has one.
    try:
        if os.path.exists(_DB_CONFIG_PATH):
            with open(_DB_CONFIG_PATH, encoding='utf-8') as fh:
                cfg = json.load(fh)
            cfg_center_name = str(cfg.get('center_name') or '').strip()
            if cfg_center_name:
                return cfg_center_name
    except Exception:
        pass

    try:
        from modules import instructor_profile_manager

        profile = instructor_profile_manager.get_instructor_profile()
        if profile:
            # Prefer the explicit center name/location field used in the UI.
            for key in ('center_location', 'name', 'center_address'):
                value = str(profile.get(key) or '').strip()
                if value:
                    return value
    except Exception:
        pass

    return 'Stdytime Center'


def render_branded_email_shell(title: str, center_name: Optional[str], body_html: str,
                               footer_note: Optional[str] = None,
                               subtitle: Optional[str] = None) -> str:
    """Wrap email body content in the shared Stdytime green/yellow email shell."""
    safe_center_name = resolve_center_name(center_name=center_name)
    if subtitle and str(subtitle).strip():
        safe_subtitle = str(subtitle).strip()
    else:
        safe_subtitle = safe_center_name

    if footer_note and str(footer_note).strip():
        safe_footer_note = str(footer_note).strip()
    else:
        safe_footer_note = f'This is an automated message from {safe_center_name}. Please do not reply to this email.'

    # Replace any stale/hardcoded center name remnants in caller-provided text.
    for legacy_label in ("KUMON PLANTATION SOUTH", "Kumon Plantation South"):
        safe_subtitle = safe_subtitle.replace(legacy_label, safe_center_name)
        safe_footer_note = safe_footer_note.replace(legacy_label, safe_center_name)

    return f"""
<html>
<head>
    <style>
        body {{ margin: 0; padding: 0; background: {BRAND_SURFACE}; font-family: Arial, sans-serif; color: {BRAND_TEXT}; }}
        .shell {{ width: 100%; padding: 24px 12px; box-sizing: border-box; }}
        .card {{ max-width: 680px; margin: 0 auto; background: #ffffff; border: 1px solid {BRAND_BORDER}; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 24px rgba(27, 94, 32, 0.12); }}
        .header {{ background: linear-gradient(135deg, {BRAND_PRIMARY_DARK} 0%, {BRAND_PRIMARY} 65%, #4caf50 100%); color: white; padding: 24px; text-align: center; }}
        .header h2 {{ margin: 0; font-size: 24px; font-weight: 700; }}
        .header p {{ margin: 8px 0 0; color: {BRAND_ACCENT}; font-size: 14px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }}
        .content {{ padding: 24px; }}
        .highlight {{ margin: 0 0 18px; padding: 12px 14px; border-radius: 10px; background: #fff8cf; border: 1px solid #f5de72; color: {BRAND_PRIMARY_DARK}; font-weight: 600; }}
        .report-table {{ width: 100%; border-collapse: collapse; margin-top: 18px; overflow: hidden; border-radius: 10px; }}
        .report-table th, .report-table td {{ border: 1px solid {BRAND_BORDER}; padding: 12px; text-align: left; }}
        .report-table th {{ background: {BRAND_SURFACE_STRONG}; color: {BRAND_PRIMARY_DARK}; font-weight: 700; width: 42%; }}
        .footer {{ margin-top: 28px; padding-top: 18px; border-top: 1px solid {BRAND_BORDER}; color: {BRAND_MUTED}; font-size: 12px; line-height: 1.6; }}
        .footer strong {{ color: {BRAND_PRIMARY_DARK}; }}
    </style>
</head>
<body>
    <div class="shell">
        <div class="card">
            <div class="header">
                <h2>{title}</h2>
                <p>{safe_subtitle}</p>
            </div>
            <div class="content">
                {body_html}
                <div class="footer">
                    <p><strong>{safe_center_name}</strong></p>
                    <p>{safe_footer_note}</p>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""


class EmailManager:
    """Manages email sending operations"""
    
    def __init__(self, smtp_server: str = None, smtp_port: int = None, 
                 sender_email: str = None, sender_password: str = None):
        """
        Initialize email manager with SMTP configuration
        
        Args:
            smtp_server: SMTP server address (e.g., 'smtp.gmail.com')
            smtp_port: SMTP port (587 for TLS, 465 for SSL)
            sender_email: Sender email address
            sender_password: Sender email password or app-specific password
        """
        self.smtp_server = smtp_server or os.getenv('SMTP_SERVER', 'smtp.gmail.com')
        self.smtp_port = smtp_port or int(os.getenv('SMTP_PORT', '587'))
        self.sender_email = sender_email or os.getenv('SENDER_EMAIL', '')
        self.sender_password = sender_password or os.getenv('SENDER_PASSWORD', '')
    
    def send_email(self, recipient_email: str, subject: str, body: str, 
                   html_body: Optional[str] = None, 
                   attachments: Optional[List[str]] = None,
                   no_reply: bool = False,
                   from_email: Optional[str] = None) -> Dict[str, any]:
        """
        Send an email
        
        Args:
            recipient_email: Recipient's email address
            subject: Email subject
            body: Plain text email body
            html_body: Optional HTML version of the email body
            attachments: Optional list of file paths to attach
            no_reply: If True, prevents recipients from replying (sets headers to block replies)
            
        Returns:
            Dictionary with 'success' status and 'message' or 'error'
        """
        try:
            # Validate configuration
            if not self.sender_email or not self.sender_password:
                return {
                    'success': False,
                    'error': 'Email configuration not set. Please configure SMTP settings.'
                }
            
            # Create message
            message = MIMEMultipart('alternative')
            message['From'] = str(from_email or self.sender_email or '').strip()
            message['To'] = recipient_email
            message['Subject'] = subject
            
            # Set no-reply headers if requested
            if no_reply:
                # Set Reply-To to empty (prevents replies in most email clients)
                message['Reply-To'] = ''
                # Prevent auto-replies from mail servers
                message['Precedence'] = 'bulk'
                # Hide unsubscribe option (professional approach)
                message['List-Unsubscribe'] = '<mailto:noreply@stdytime.com>'
            
            # Attach plain text body
            part1 = MIMEText(body, 'plain')
            message.attach(part1)
            
            # Attach HTML body if provided
            if html_body:
                part2 = MIMEText(html_body, 'html')
                message.attach(part2)
            
            # Attach files if provided
            if attachments:
                for file_path in attachments:
                    if os.path.exists(file_path):
                        with open(file_path, 'rb') as attachment:
                            part = MIMEBase('application', 'octet-stream')
                            part.set_payload(attachment.read())
                            encoders.encode_base64(part)
                            part.add_header(
                                'Content-Disposition',
                                f'attachment; filename= {os.path.basename(file_path)}',
                            )
                            message.attach(part)
            
            # Connect to server and send
            try:
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.starttls()  # Secure the connection
                    server.login(self.sender_email, self.sender_password)
                    server.send_message(message)
            except smtplib.SMTPAuthenticationError:
                # Common issue: Gmail app passwords are often copied with spaces for readability.
                # Retry once with internal spaces removed.
                compact_password = (self.sender_password or '').replace(' ', '')
                if compact_password and compact_password != self.sender_password:
                    with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                        server.starttls()
                        server.login(self.sender_email, compact_password)
                        server.send_message(message)
                else:
                    raise
            
            return {
                'success': True,
                'message': f'Email sent successfully to {recipient_email}'
            }
            
        except smtplib.SMTPAuthenticationError:
            return {
                'success': False,
                'error': 'Authentication failed. Please check SENDER_EMAIL/SENDER_PASSWORD (for Gmail app password, try without spaces).'
            }
        except smtplib.SMTPException as e:
            return {
                'success': False,
                'error': f'SMTP error: {str(e)}'
            }
        except Exception as e:
            return {
                'success': False,
                'error': f'Error sending email: {str(e)}'
            }
    
    def send_report_card(self, student_name: str, recipient_email: str,
                        report_data: Dict[str, any],
                        center_name: Optional[str] = None) -> Dict[str, any]:
        """
        Send student report card via email
        
        Args:
            student_name: Student's full name
            recipient_email: Recipient's email address
            report_data: Dictionary containing report information
            
        Returns:
            Dictionary with 'success' status and 'message' or 'error'
        """
        subject = f"Student Report Card - {student_name}"
        center_name = resolve_center_name(center_name=center_name)
        # Create plain text body
        body = f"""
Dear Parent/Guardian,

Please find below the report card for {student_name}.

*** DO NOT REPLY TO THIS EMAIL ***

REPORT SUMMARY:
---------------
Subject: {report_data.get('subject', 'N/A')}
Date Range: {report_data.get('start_date', 'N/A')} to {report_data.get('end_date', 'N/A')}
Highest Worksheet Completed: {report_data.get('highest_ws_completed', 'N/A')}
Number of Worksheets: {report_data.get('num_ws', 'N/A')}
Study Days: {report_data.get('study_days', 'N/A')}
Cumulative Study Time: {report_data.get('cum_study_time', 'N/A')}
Current Subject Status: {report_data.get('current_subject_status', 'N/A')}

Best regards,
{center_name}
"""
        
        # Create HTML body
        html_body = render_branded_email_shell(
            title="🎓 Student Report Card",
            center_name=center_name,
            subtitle=center_name,
            footer_note=f"This is an automated message from {center_name}. Please do not reply to this email.",
            body_html=f"""
                <p>Dear Parent/Guardian,</p>
                <p>Please find below the report card for <strong>{student_name}</strong>.</p>
                <div class="highlight"><strong>Do not reply:</strong> This mailbox is not monitored.</div>
                <table class="report-table">
                    <tr>
                        <th>Subject</th>
                        <td>{report_data.get('subject', 'N/A')}</td>
                    </tr>
                    <tr>
                        <th>Report Date Range</th>
                        <td>{report_data.get('start_date', 'N/A')} to {report_data.get('end_date', 'N/A')}</td>
                    </tr>
                    <tr>
                        <th>Highest Worksheet Completed</th>
                        <td>{report_data.get('highest_ws_completed', 'N/A')}</td>
                    </tr>
                    <tr>
                        <th>Number of Worksheets</th>
                        <td>{report_data.get('num_ws', 'N/A')}</td>
                    </tr>
                    <tr>
                        <th>Study Days</th>
                        <td>{report_data.get('study_days', 'N/A')}</td>
                    </tr>
                    <tr>
                        <th>Cumulative Study Time</th>
                        <td>{report_data.get('cum_study_time', 'N/A')}</td>
                    </tr>
                    <tr>
                        <th>Current Subject Status</th>
                        <td>{report_data.get('current_subject_status', 'N/A')}</td>
                    </tr>
                </table>
                <p style="margin-top:16px; color:{BRAND_MUTED};">For any questions, please contact your institution.</p>
            """
        )
        
        # Send as no-reply (prevents recipients from replying)
        return self.send_email(recipient_email, subject, body, html_body, no_reply=True)


def get_email_manager() -> EmailManager:
    """Factory function to get configured EmailManager instance"""
    return EmailManager()
