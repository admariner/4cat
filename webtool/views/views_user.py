"""
Control access to web tool - views and functions used in handling user access
"""
import html2text
import requests
import smtplib
import fnmatch
import socket
import random
import time

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app, Blueprint, request, abort, render_template, redirect, url_for, flash, get_flashed_messages, jsonify, g
from flask_login import login_user, login_required, logout_user, current_user
from common.lib.user import User
from webtool.lib.helpers import error, generate_css_colours, setting_required
from common.lib.helpers import send_email, get_software_commit
from common.config_manager import ConfigWrapper

from pathlib import Path

component = Blueprint("user", __name__)

# ok to use the app's config reader here since this is a global-only setting
access_request_limit = current_app.fourcat_config.get("4cat.allow_access_request_limiter", default="100/day")

@current_app.login_manager.user_loader
def load_user(user_name):
    """
    Load user object

    Required for Flask-Login.
    :param user_name:  ID of user
    :return:  User object
    """
    user = User.get_by_name(current_app.db, user_name)
    if user:
        user.authenticate()

    return user


@current_app.login_manager.request_loader
def load_user_from_request(request):
    """
    Load user object via access token

    Access token may be supplied via a GET parameter or the Authorization
    HTTP header.

    :param request:  Flask request
    :return:  User object, or None if no valid access token was given
    """
    token = request.args.get("access-token")

    if not token:
        token = request.headers.get("Authentication")

    if not token:
        # this was a mistake, but was being checked for a long time, so we're
        # keeping it for legacy support
        token = request.headers.get("Authorization")

    if not token:
        return None

    user = current_app.db.fetchone("SELECT name AS user FROM access_tokens WHERE token = %s AND (expires = 0 OR expires > %s)",
                       (token, int(time.time())))
    if not user:
        return None
    else:
        current_app.db.execute("UPDATE access_tokens SET calls = calls + 1 WHERE name = %s", (user["user"],))
        user = User.get_by_name(current_app.db, user["user"])
        user.authenticate()
        user.with_config(ConfigWrapper(current_app.fourcat_config, user=user, request=request))
        return user

@current_app.login_manager.unauthorized_handler
def unauthorized():
    """
    Handle unauthorized requests

    Shows an error message, or if the user is not logged in yet redirects them
    to the login page.
    """
    if current_user.is_authenticated:
        return render_template("error.html", message="You cannot view this page."), 403
    else:
        return redirect(url_for("user.show_login"))


@component.before_request
def reroute_requests():
    """
    Sometimes the requested route should be overruled
    """

    # Displays a 'sorry, no 4cat for you' message to banned or deactivated users.
    if current_user and current_user.is_authenticated and current_user.is_deactivated:
        message = "Your 4CAT account has been deactivated and you can no longer access this page."
        if current_user.get_value("deactivated.reason"):
            message += "\n\nThe following reason was recorded for your account's deactivation: *"
            message += current_user.get_value("deactivated.reason") + "*"

        return render_template("error.html", title="Your account has been deactivated", message=message), 403

    # ensures admins get to see the phone home screen at least once
    elif current_user and current_user.is_authenticated and current_user.is_admin and \
            request.url_rule and request.url_rule.endpoint not in ("static", "first_run_dialog"):
        wants_phone_home = not g.config.get("4cat.phone_home_asked", False)
        if wants_phone_home:
            return redirect(url_for("user.first_run_dialog"))


@component.before_app_request
def autologin_whitelist():
    """
    Checks if host name matches whitelisted hostmask or IP. If so, the user is
    logged in as the special "autologin" user.
    """
    # ok to use the app's config reader here since this is a global-only setting
    if not current_app.autologin.hostnames:
        # if there's no whitelist, there's no point in checking it
        return

    if "/static/" in request.path:
        # never check login for static files
        return

    # filter by IP address and hostname, if the latter is available
    filterables = [request.remote_addr]
    try:
        socket.setdefaulttimeout(2)
        hostname = socket.gethostbyaddr(request.remote_addr)[0]
        filterables.append(hostname)
    except (socket.herror, socket.timeout):
        pass  # no hostname for this address

    if current_user:
        if current_user.get_id() == "autologin":
            # whitelist should be checked on every request
            logout_user()
        elif current_user.is_authenticated:
            # if we're logged in as a regular user, no need for a check
            return

    # autologin is a special user that is automatically logged in for this
    # request only if the hostname or IP matches the whitelist
    if any([fnmatch.filter(filterables, hostmask) for hostmask in current_app.autologin.hostnames]):
        autologin_user = User.get_by_name(current_app.db, "autologin")
        
        if not autologin_user:
            # this user should exist by default
            abort(500)
        autologin_user.authenticate()
        autologin_user.with_config(ConfigWrapper(current_app.fourcat_config, user=autologin_user, request=request))
        login_user(autologin_user, remember=False)


@current_app.limiter.request_filter
def exempt_from_limit():
    """
    Checks if host name matches whitelisted hostmasks for exemption from API
    rate limiting.

    :return bool:  Whether the request's hostname is exempt
    """
    # ok to use the app's config reader here since this is a global-only setting
    if not current_app.autologin.api:
        return False

    # filter by IP address and hostname, if the latter is available
    filterables = [request.remote_addr]
    try:
        socket.setdefaulttimeout(2)
        hostname = socket.gethostbyaddr(request.remote_addr)[0]
        filterables.append(hostname)
    except (socket.herror, socket.timeout):
        pass  # no hostname for this address

    if any([fnmatch.filter(filterables, hostmask) for hostmask in current_app.autologin.api]):
        return True

    return False


@component.route("/first-run/", methods=["GET", "POST"])
def first_run_dialog():
    """
    Special route for creating an initial admin user

    This route is only available if there are no admin users in the database
    yet. The user created through this route is always made an admin.

    :return:
    """
    has_admin_user = g.db.fetchone("SELECT COUNT(*) AS amount FROM users WHERE tags @> '[\"admin\"]'")["amount"]
    wants_phone_home = not g.config.get("4cat.phone_home_asked", False)

    if has_admin_user and not wants_phone_home:
        return error(403, message="The 'first run' page is not available")

    version_file = g.config.get("PATH_CONFIG").joinpath(".current-version")

    if version_file.exists():
        with version_file.open() as infile:
            version = infile.readline().strip()
    else:
        version = "unknown"

    missing = []
    
    # choose a random adjective to differentiate this 4CAT instance (this can
    # be edited by the user)
    adjective_file = Path(g.config.get("PATH_ROOT"), "common/assets/wordlists/positive-adjectives.txt")
    if not adjective_file.exists():
        adjectives = ["Awesome"]
    else:
        with adjective_file.open() as infile:
            adjectives = [line.strip().title() for line in infile.readlines()]
    adjective = random.choice(adjectives)

    # choose a random accent colour (this can also be edited)
    interface_hue = random.random()

    phone_home_url = g.config.get("4cat.phone_home_url")
    if request.method == 'GET':
        template = "account/first-run.html" if not has_admin_user else "account/first-run-after-update.html"
        return render_template(template, incomplete=missing, form=request.form, phone_home_url=phone_home_url,
                               version=version, adjective=adjective, interface_hue=interface_hue)

    if not has_admin_user:
        username = request.form.get("username").strip()
        password = request.form.get("password").strip()
        instance_name = request.form.get("4cat_name").strip()
        interface_hue = request.form.get("interface_hue").strip()
        confirm_password = request.form.get("confirm_password").strip()

        if not username:
            missing.append("username")
        else:
            user_exists = g.db.fetchone("SELECT name FROM users WHERE name = %s", (username,))
            if user_exists:
                flash("The username '%s' already exists and is reserved." % username)
                missing.append("username")

        if not password:
            missing.append("password")
        elif password != confirm_password:
            flash("The passwords provided do not match")
            missing.append("password")
            missing.append("confirm_password")

        if not instance_name:
            missing.append("4cat_name")

        if missing:
            flash("Please make sure all fields are complete")
            return render_template("account/first-run.html", form=request.form, incomplete=missing,
                                   flashes=get_flashed_messages(), phone_home_url=phone_home_url,
                                   adjective=adjective, interface_hue=interface_hue)

        g.db.insert("users", data={"name": username, "timestamp_created": int(time.time())})
        g.db.commit()
        user = User.get_by_name(db=g.db, name=username)
        user.with_config(g.config)
        user.set_password(password)
        user.add_tag("admin")  # first user is always admin

        g.config.set("4cat.name_long", instance_name)

        # handle hue colour
        try:
            interface_hue = int(interface_hue)
            interface_hue = interface_hue if 0 <= interface_hue <= 360 else random.randrange(0, 360)
        except (ValueError, TypeError):
            interface_hue = random.randrange(0, 360)

        g.config.set("4cat.layout_hue", interface_hue)
        generate_css_colours(config=g.config, force=True)

        # make user an admin
        g.db.update("users", where={"name": username}, data={"is_deactivated": False})
        g.db.commit()

        flash("The admin user '%s' was created, you can now use it to log in." % username)

    if phone_home_url and request.form.get("phonehome"):
        with g.config.get("PATH_CONFIG").joinpath(".current-version").open() as outfile:
            version = outfile.read(64).split("\n")[0].strip()

        payload = {
            "version": version,
            "commit": get_software_commit(),
            "role": request.form.get("role", ""),
            "affiliation": request.form.get("affiliation", ""),
            "email": request.form.get("email", "")
        }

        try:
            requests.post(phone_home_url, payload, timeout=5)
        except requests.RequestException:
            # too bad
            flash("Could not send install ping to 4CAT developers")

    # don't ask phone home again until next update
    g.config.set("4cat.phone_home_asked", True)

    redirect_path = "user.show_login" if not has_admin_user else "misc.show_frontpage"
    return redirect(url_for(redirect_path))


@component.route('/login/', methods=['GET', 'POST'])
def show_login():
    """
    Handle logging in

    If not submitting a form, show form; if submitting, check if login is valid
    or not.

    :return: Redirect to either the URL form, or the index (if logged in)
    """
    if current_user.is_authenticated:
        return redirect(url_for("misc.show_frontpage"))

    has_admin_user = g.db.fetchone("SELECT * FROM users WHERE tags @> '[\"admin\"]'")
    if not has_admin_user:
        return redirect(url_for("user.first_run_dialog"))

    have_email = g.config.get('mail.admin_email') and g.config.get('mail.server')
    if request.method == 'GET':
        return render_template('account/login.html', flashes=get_flashed_messages(), have_email=have_email), 401

    username = request.form['username']
    password = request.form['password']
    registered_user = User.get_by_login(g.db, username, password)

    if registered_user is None:
        flash('Username or Password is invalid.', 'error')
        return redirect(url_for('user.show_login'))

    registered_user.with_config(g.config)
    login_user(registered_user, remember=True)

    return redirect(url_for("misc.show_frontpage"))


@component.route("/logout")
@login_required
def logout():
    """
    Log a user out

    :return:  Redirect to URL form
    """
    logout_user()
    flash("You have been logged out of 4CAT.")
    return redirect(url_for("user.show_login"))


@component.route("/request-access/", methods=["GET", "POST"])
@setting_required("4cat.allow_access_request")
@current_app.limiter.limit(access_request_limit, methods=["POST"])
def request_access():
    """
    Request a 4CAT Account

    Displays a web form for people to fill in their details which can then be
    sent to the 4CAT admin via e-mail so they can create an account (if
    approved)
    """
    if not g.config.get('mail.admin_email'):
        return render_template("error.html",
                               message="No administrator e-mail is configured; the request form cannot be displayed.")

    if not g.config.get('mail.server'):
        return render_template("error.html",
                               message="No e-mail server configured; the request form cannot be displayed.")

    if current_user.is_authenticated:
        return render_template("error.html", message="You are already logged in and cannot request another account.")

    incomplete = []

    policy_template = Path(g.config.get('PATH_ROOT'), "webtool/pages/access-policy.md")
    access_policy = ""
    if policy_template.exists():
        access_policy = policy_template.read_text(encoding="utf-8")

    if request.method == "POST":
        required = ("name", "email", "university", "intent", "source")
        for field in required:
            if not request.form.get(field, "").strip():
                incomplete.append(field)

        if incomplete:
            flash("Please fill in all fields before submitting.")
        else:
            html_parser = html2text.HTML2Text()

            sender = g.config.get('mail.noreply')
            message = MIMEMultipart("alternative")
            message["Subject"] = "Account request"
            message["From"] = sender
            message["To"] = g.config.get('mail.admin_email', "")

            mail = "<p>Hello! Someone requests a 4CAT Account:</p>\n"
            for field in required:
                mail += "<p><b>" + field + "</b>: " + request.form.get(field, "") + " </p>\n"

            root_url = "https" if g.config.get("flask.https") else "http"
            root_url += "://%s/admin/" % g.config.get("flask.server_name")
            approve_url = root_url + "add-user/?format=html&email=%s" % request.form.get("email", "")
            reject_url = root_url + "reject-user/?name=%s&email=%s" % (
            request.form.get("name", ""), request.form.get("email", ""))
            mail += "<p>Use <a href=\"%s\">this link</a> to approve this request and send a password reset e-mail.</p>" % approve_url
            mail += "<p>Use <a href=\"%s\">this link</a> to send a message to this person about why their request was " \
                    "rejected.</p>" % reject_url

            message.attach(MIMEText(html_parser.handle(mail), "plain"))
            message.attach(MIMEText(mail, "html"))

            try:
                send_email(g.config.get('mail.admin_email'), message, g.config)
                return render_template("error.html", title="Thank you",
                                       message="Your request has been submitted; we'll try to answer it as soon as possible.")
            except (smtplib.SMTPException, ConnectionRefusedError, socket.timeout):
                return render_template("error.html", title="Error",
                                       message="The form could not be submitted; the e-mail server is unreachable.")

    return render_template("account/request.html", incomplete=incomplete, flashes=get_flashed_messages(),
                           form=request.form, access_policy=access_policy)


@component.route("/reset-password/", methods=["GET", "POST"])
def reset_password():
    """
    Reset a password

    This page requires a valid reset token to be supplied as a GET parameter.
    If that is satisfied then the user may choose a new password which they can
    then use to log in.
    """
    if current_user.is_authenticated:
        # this makes no sense if you're already logged in
        return render_template("error.html", message="You are already logged in and cannot request another account.")

    token = request.args.get("token", None) or request.form.get("token", None)
    if token is None:
        # we need *a* token
        return render_template("error.html", message="You need a valid reset token to set a password.")

    resetting_user = User.get_by_token(g.db, token)
    if not resetting_user or resetting_user.is_special:
        # this doesn't mean the token is unknown, but it could be older than 3 days
        return render_template("error.html",
                               message="You need a valid reset token to set a password. Your token may have expired: in this case, you have to request a new one.")

    # check form
    resetting_user.with_config(g.config)
    incomplete = []
    if request.method == "POST":
        # check password validity
        password = request.form.get("password", None)
        if password is None or len(password) < 8:
            incomplete.append("password")
            flash("Please provide a password of at least 8 characters.")

        # reset password if okay and redirect to login
        if not incomplete:
            resetting_user.set_password(password)
            resetting_user.clear_token()
            flash("Your password has been set. You can now log in to 4CAT.")
            return redirect(url_for("user.show_login"))

    # show form
    return render_template("account/reset-password.html", username=resetting_user.get_name(), incomplete=incomplete,
                           flashes=get_flashed_messages(), token=token,
                           form=request.form)


@component.route("/request-password/", methods=["GET", "POST"])
@current_app.limiter.limit("6 per minute")
def request_password():
    """
    Request a password reset

    A user that is not logged in can use this page to request that a password
    reset link will be sent to them. Only one link can be requested per 3 days.

    This view is rate-limited to prevent brute forcing a list of e-mails.
    :return:
    """
    if current_user.is_authenticated:
        # using this while logged in makes no sense
        return render_template("error.html", message="You are already logged in and cannot request a password reset.")

    # check form submission
    incomplete = []
    if request.method == "POST":
        # we need *a* username
        username = request.form.get("username", None)
        if username is None:
            incomplete.append(username)
            flash("Please provide a username.")

        # is it also a valid username? that is not a 'special' user (like autologin)?
        resetting_user = User.get_by_name(g.db, username)

        if resetting_user is None or resetting_user.is_special:
            incomplete.append("username")
            flash("That user is not known here. Note that your username is typically your e-mail address.")

        elif resetting_user.get_token() and resetting_user.data["timestamp_token"] > 0 and resetting_user.data[
            "timestamp_token"] > time.time() - (3 * 86400):
            # and have they not already requested a reset?
            incomplete.append("")
            flash(
                "You have recently requested a password reset and an e-mail has been sent to you containing a reset link. It could take a while to arrive; also, don't forget to check your spam folder.")
        else:
            # okay, send an e-mail
            resetting_user.with_config(g.config)
            try:
                resetting_user.email_token(new=False)
                return render_template("error.html", title="Success",
                                       message="An e-mail has been sent to you containing instructions on how to reset your password.")
            except RuntimeError:
                # no e-mail could be sent - clear the token so the user can try again later
                resetting_user.clear_token()
                incomplete.append(username)
                flash("The username was recognised but no reset e-mail could be sent. Please try again later.")

    # show page
    return render_template("account/request-password.html", incomplete=incomplete, flashes=get_flashed_messages(),
                           form=request.form)



@component.route("/access-tokens/")
@login_required
def show_access_tokens():
    user = current_user.get_id()

    if user == "autologin":
        return error(403, message="You cannot view or generate access tokens without a personal acount.")

    tokens = g.db.fetchall("SELECT * FROM access_tokens WHERE name = %s", (user,))

    return render_template("access-tokens.html", tokens=tokens)

@component.route("/dismiss-notification/<int:notification_id>")
def dismiss_notification(notification_id):
    current_user.dismiss_notification(notification_id)

    if not request.args.get("async"):
        redirect_url = request.headers.get("Referer")
        if not redirect_url:
            redirect_url = url_for("misc.show_frontpage")

        return redirect(redirect_url)
    else:
        return jsonify({"success": True})
