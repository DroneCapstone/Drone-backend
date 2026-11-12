import httpx
from app.core.config import settings


def send_reset_email(to_email: str, reset_link: str):
    body = f"""
    <h3>Reset Your DroneOptAI Password</h3>
    <p>Click the link below to reset your password (expires in 15 minutes):</p>
    <a href="{reset_link}">{reset_link}</a>
    <p>If you did not request this, please ignore this email.</p>
    """
    response = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": settings.BREVO_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "sender": {"name": "DroneOptAI", "email": settings.EMAIL_FROM},
            "to": [{"email": to_email}],
            "subject": "Reset Your DroneOptAI Password",
            "htmlContent": body,
        },
        timeout=10,
    )
    response.raise_for_status()


async def send_mission_scheduled_email(
    to_email: str,
    order_id: str,
    mission_id: str | None = None,
    frontend_url: str | None = None,
) -> bool:
    """
    Gửi email thông báo đơn hàng đã được lên lịch bay (operator tạo mission).
    """
    tracking_url = f"{frontend_url}/orders/{order_id}/tracking" if frontend_url else ""
    tracking_section = f"""
        <p>
            <a href="{tracking_url}" style="
                display: inline-block;
                padding: 10px 20px;
                background-color: #7c3aed;
                color: #ffffff;
                text-decoration: none;
                border-radius: 6px;
                font-weight: bold;
            ">Track Order</a>
        </p>
    """ if tracking_url else ""

    mission_line = f"<p><b>Mission ID:</b> {mission_id}</p>" if mission_id else ""

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #7c3aed; padding: 20px; text-align: center;">
            <h1 style="color: #ffffff; margin: 0;">📅 DroneOptAI</h1>
        </div>
        <div style="padding: 24px; background-color: #f9fafb; border: 1px solid #e5e7eb;">
            <h2 style="color: #111827;">Your Order Has Been Scheduled for Delivery!</h2>
            <p>Hello,</p>
            <p>Your order has been confirmed and scheduled for a drone delivery mission.</p>
            <p><b>Order ID:</b> {order_id}</p>
            {mission_line}
            <p>The drone will take off once the origin station confirms package drop-off.</p>
            {tracking_section}
            <p style="color: #6b7280; font-size: 13px; margin-top: 24px;">
                If you have any questions, please contact support via this email.
            </p>
        </div>
        <div style="padding: 12px; text-align: center; color: #9ca3af; font-size: 12px;">
            DroneOptAI — Drone Delivery System
        </div>
    </div>
    """

    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": settings.BREVO_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "sender": {"name": "DroneOptAI", "email": settings.EMAIL_FROM},
                "to": [{"email": to_email}],
                "subject": f"[DroneOptAI] Order {order_id} has been scheduled for flight 📅",
                "htmlContent": html_body,
            },
            timeout=10,
        )
        response.raise_for_status()
        print(f"[EMAIL] Gửi mission scheduled thành công đến {to_email}")
        return True
    except Exception as e:
        print(f"[EMAIL] Lỗi gửi mission scheduled email: {e}")
        return False


async def send_delivery_email(
    to_email: str,
    order_id: str,
    mission_code: str | None = None,
    frontend_url: str | None = None,
) -> bool:
    """
    Gửi email thông báo đơn hàng đang được drone giao.
    Trả về True nếu thành công, False nếu lỗi (không throw).
    """
    tracking_url = f"{frontend_url}/orders/{order_id}/tracking" if frontend_url else ""

    tracking_section = f"""
        <p>
            <a href="{tracking_url}" style="
                display: inline-block;
                padding: 10px 20px;
                background-color: #2563eb;
                color: #ffffff;
                text-decoration: none;
                border-radius: 6px;
                font-weight: bold;
            ">Track Order</a>
        </p>
    """ if tracking_url else ""

    mission_line = f"<p><b>Mission Code:</b> {mission_code}</p>" if mission_code else ""

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #2563eb; padding: 20px; text-align: center;">
            <h1 style="color: #ffffff; margin: 0;">🚁 DroneOptAI</h1>
        </div>
        <div style="padding: 24px; background-color: #f9fafb; border: 1px solid #e5e7eb;">
            <h2 style="color: #111827;">Your Order Is Out for Delivery!</h2>
            <p>Hello,</p>
            <p>Your order is currently being delivered by drone to the destination hub.</p>
            <p><b>Order ID:</b> {order_id}</p>
            {mission_line}
            {tracking_section}
            <p style="color: #6b7280; font-size: 13px; margin-top: 24px;">
                If you have any questions, please contact support via this email.
            </p>
        </div>
        <div style="padding: 12px; text-align: center; color: #9ca3af; font-size: 12px;">
            DroneOptAI — Drone Delivery System
        </div>
    </div>
    """

    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": settings.BREVO_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "sender": {"name": "DroneOptAI", "email": settings.EMAIL_FROM},
                "to": [{"email": to_email}],
                "subject": f"[DroneOptAI] Order {order_id} is out for delivery 🚁",
                "htmlContent": html_body,
            },
            timeout=10,
        )
        response.raise_for_status()
        print(f"[EMAIL] Gửi thành công đến {to_email}")
        return True
    except Exception as e:
        print(f"[EMAIL] Lỗi gửi email: {e}")
        return False


async def send_hub_arrival_email(
    to_email: str,
    order_id: str,
    hub_name: str | None = None,
    frontend_url: str | None = None,
) -> bool:
    """
    Gửi email thông báo đơn hàng đã đến trạm hub đích, chờ khách đến nhận.
    """
    hub_line = f"<p><b>Pickup Hub:</b> {hub_name}</p>" if hub_name else ""
    tracking_url = f"{frontend_url}/orders/{order_id}/tracking" if frontend_url else ""
    tracking_section = f"""
        <p>
            <a href="{tracking_url}" style="
                display: inline-block;
                padding: 10px 20px;
                background-color: #16a34a;
                color: #ffffff;
                text-decoration: none;
                border-radius: 6px;
                font-weight: bold;
            ">View Order Details</a>
        </p>
    """ if tracking_url else ""

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #16a34a; padding: 20px; text-align: center;">
            <h1 style="color: #ffffff; margin: 0;">✅ DroneOptAI</h1>
        </div>
        <div style="padding: 24px; background-color: #f9fafb; border: 1px solid #e5e7eb;">
            <h2 style="color: #111827;">Your Order Has Arrived!</h2>
            <p>Hello,</p>
            <p>The drone has successfully delivered your order to the destination hub. Please pick up your package soon.</p>
            <p><b>Order ID:</b> {order_id}</p>
            {hub_line}
            {tracking_section}
            <p style="color: #dc2626; font-size: 13px; margin-top: 12px;">
                ⚠️ Your order will be cancelled if it is not picked up within 5 days.
            </p>
            <p style="color: #6b7280; font-size: 13px; margin-top: 12px;">
                If you have any questions, please contact support via this email.
            </p>
        </div>
        <div style="padding: 12px; text-align: center; color: #9ca3af; font-size: 12px;">
            DroneOptAI — Drone Delivery System
        </div>
    </div>
    """

    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": settings.BREVO_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "sender": {"name": "DroneOptAI", "email": settings.EMAIL_FROM},
                "to": [{"email": to_email}],
                "subject": f"[DroneOptAI] Order {order_id} has arrived at the pickup hub 📦",
                "htmlContent": html_body,
            },
            timeout=10,
        )
        response.raise_for_status()
        print(f"[EMAIL] Gửi hub arrival thành công đến {to_email}")
        return True
    except Exception as e:
        print(f"[EMAIL] Lỗi gửi hub arrival email: {e}")
        return False
