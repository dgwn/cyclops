import json
import os
import requests

from flask import (
    Flask,
    redirect,
    request,
    render_template,
    session,
    url_for,
    jsonify,
    send_file,
)
from flask_sqlalchemy import SQLAlchemy
from flask_caching import Cache
from flask_migrate import Migrate
from werkzeug.utils import secure_filename
from pylti1p3.contrib.flask import (
    FlaskOIDCLogin,
    FlaskMessageLaunch,
    FlaskRequest,
    FlaskCacheDataStorage,
)
from pylti1p3.exception import LtiException
from pylti1p3.tool_config import ToolConfDict
from pylti1p3.deep_link_resource import DeepLinkResource

from urllib.parse import urlparse
from PIL import Image
import pytesseract
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

from config import API_URL, API_KEY


class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        scheme = environ.get("HTTP_X_FORWARDED_PROTO")
        if scheme:
            environ["wsgi.url_scheme"] = scheme
        return self.app(environ, start_response)


app = Flask("lti-13-example", template_folder="templates")
app.config.from_pyfile("config.py")
app.secret_key = app.config["SECRET_KEY"]
app.wsgi_app = ReverseProxied(app.wsgi_app)
cache = Cache(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)

canvas = Canvas(API_URL, API_KEY)

# ============================================
# Extended Classes in pylti1p3 lib
# ============================================


class ExtendedFlaskMessageLaunch(FlaskMessageLaunch):
    def validate_nonce(self):
        """
        Probably it is bug on "https://lti-ri.imsglobal.org":
        site passes invalid "nonce" value during deep links launch.
        Because of this in case of iss == http://imsglobal.org just skip nonce validation.

        """
        iss = self.get_iss()
        deep_link_launch = self.is_deep_link_launch()

        if iss == "http://imsglobal.org" and deep_link_launch:
            return self
        return super(ExtendedFlaskMessageLaunch, self).validate_nonce()

    def validate_deployment(self):
        iss = self._get_iss()
        deployment_id = self._get_deployment_id()
        tool_conf = get_lti_config(session["iss"], session["client_id"])

        # Find deployment.
        deployment = self._tool_config.find_deployment(iss, deployment_id)
        if deployment_id in tool_conf._config[iss][0]["deployment_ids"][0]:
            deployment = True
        if not deployment:
            raise LtiException("Unable to find deployment")

        return self


# ============================================
# DB Models
# ============================================


class LTIConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    iss = db.Column(db.Text)
    client_id = db.Column(db.Text)
    auth_login_url = db.Column(db.Text)
    auth_token_url = db.Column(db.Text)
    key_set_url = db.Column(db.Text)
    private_key_file = db.Column(db.Text)
    public_key_file = db.Column(db.Text)
    public_jwk = db.Column(db.Text)
    deployment_id = db.Column(db.Text)


def get_lti_config(iss, client_id):
    lti = LTIConfig.query.filter_by(iss=iss, client_id=client_id).first()

    settings = {
        lti.iss: [
            {
                "client_id": lti.client_id,
                "auth_login_url": lti.auth_login_url,
                "auth_token_url": lti.auth_token_url,
                "auth_audience": "null",
                "key_set_url": lti.key_set_url,
                "key_set": None,
                "deployment_ids": [lti.deployment_id],
            }
        ]
    }

    private_key = lti.private_key_file
    public_key = lti.public_key_file
    tool_conf = ToolConfDict(settings)

    tool_conf.set_private_key(iss, private_key, client_id=client_id)
    tool_conf.set_public_key(iss, public_key, client_id=client_id)

    return tool_conf


# ============================================
# Utilities
# ============================================


@app.context_processor
def utility_processor():
    def google_analytics():
        return app.config["GOOGLE_ANALYTICS"]

    return dict(google_analytics=google_analytics())


def get_launch_data_storage():
    return FlaskCacheDataStorage(cache)


def create_tree(folder_list):
    # Put folders in a dictionary to make it easier to put files into them
    root_folder = None
    folder_dict = {}
    for folder in folder_list:
        if folder["parent_folder_id"] is None:
            root_folder = folder
        else:
            folder_dict[folder["id"]] = folder

    # Put folders in their parent folders
    for folder in folder_dict.values():
        if folder["parent_folder_id"] == root_folder["id"]:
            root_folder["folders"].append(folder)
        else:
            folder_dict[folder["parent_folder_id"]]["folders"].append(folder)

    final_output = (
        '<ol class="tree"><li><label'
        f' for="{root_folder["id"]}">{root_folder["name"]}</label><input'
        f' type="checkbox" checked disabled id="{root_folder["id"]}"'
        f" /><ol>{display_children(root_folder)}</ol></li></ol>"
    )
    return final_output


def display_children(tree):
    if len(tree["folders"]) == 0 and len(tree["files"]) == 0:
        return "<li>No image files.</li>"
    output = ""
    for folder in tree["folders"]:
        output += (
            f'<li><label for="{folder["id"]}">{folder["name"]}</label><input'
            f' type="checkbox" id="{folder["id"]}" /><ol>'
        )
        # Recursively display children
        output += display_children(folder)
        output += "</ol></li>"
    for file in tree["files"]:
        output += (
            '<li class="file" onclick=showLoad()><a class="file-link"'
            f' href="/cyclops/load?filename={file["filename"]}&'
            f'fileid={file["id"]}">{file["name"]}</a></li>'
        )
    return output


# ============================================
# LTI 1.3 Routes
# ============================================


# OIDC Login
@app.route("/login/", methods=["GET", "POST"])
def login():
    session["iss"] = request.values.get("iss")
    session["client_id"] = request.values.get("client_id")

    tool_conf = get_lti_config(session["iss"], session["client_id"])

    launch_data_storage = get_launch_data_storage()

    flask_request = FlaskRequest()

    target_link_uri = flask_request.get_param("target_link_uri")
    if not target_link_uri:
        raise Exception('Missing "target_link_uri" param')

    oidc_login = FlaskOIDCLogin(
        flask_request, tool_conf, launch_data_storage=launch_data_storage
    )
    return oidc_login.enable_check_cookies(
        main_msg="Your browser prohibits saving cookies in an iframe.",
        click_msg="Click here to open the application in a new tab.",
    ).redirect(target_link_uri)


# Main Launch URL
@app.route("/launch/", methods=["POST"])
def launch():
    tool_conf = get_lti_config(session["iss"], session["client_id"])

    flask_request = FlaskRequest()
    launch_data_storage = get_launch_data_storage()
    message_launch = ExtendedFlaskMessageLaunch(
        flask_request, tool_conf, launch_data_storage=launch_data_storage
    )

    session["launch_id"] = message_launch.get_launch_id()
    session["course_id"] = message_launch.get_launch_data()[
        "https://purl.imsglobal.org/spec/lti/claim/custom"
    ]["canvas_course_id"]

    session["error"] = False

    return render_template("start.htm.j2", launch_id=session["launch_id"])


# Install JSON
@app.route("/config/<key_id>/json", methods=["GET"])
def config_json(key_id):
    title = "Cyclops LTI 1.3"
    description = "Recognize text from images"

    public_jwk = LTIConfig.query.filter_by(id=key_id).first()
    public_jwk = json.loads(public_jwk.public_jwk)

    target_link_uri = url_for("launch", _external=True)
    oidc_initiation_url = url_for("login", _external=True)

    domain = urlparse(request.url_root).netloc
    server_url = domain + urlparse(request.url_root).path

    config = {
        "title": title,
        "scopes": [],
        "extensions": [
            {
                "platform": "canvas.instructure.com",
                "settings": {
                    "platform": "canvas.instructure.com",
                    "placements": [
                        {
                            "text": "Cyclops: Text Recognition",
                            "icon_url": f"http://{server_url}icon/",
                            "placement": "editor_button",
                            "message_type": "LtiDeepLinkingRequest",
                            "selection_width": 590,
                            "target_link_uri": f"http://{server_url}launch/",
                            "selection_height": 490,
                        }
                    ],
                },
                "privacy_level": "public",
            }
        ],
        "public_jwk": public_jwk,
        "description": description,
        "custom_fields": {
            "canvas_user_id": "$Canvas.user.id",
            "canvas_course_id": "$Canvas.course.id",
        },
        "target_link_uri": target_link_uri,
        "oidc_initiation_url": oidc_initiation_url,
    }

    return jsonify(config)


@app.route("/icon/", methods=["GET"])
def icon():
    filename = "static/favicon.png"
    return send_file(filename, mimetype="image/png")


@app.route("/test/", methods=["GET"])
def test():
    filename = request.args["filename"]
    return pytesseract.image_to_string(Image.open("./images/" + filename))


# Route to view File Selector
@app.route("/select/", methods=["GET"])
def select():
    launch_id = session["launch_id"]
    course_id = session["course_id"]

    # Catch invalid tokens
    try:
        course = canvas.get_course(course_id)
    except CanvasException as e:
        print(e)
        return "<p>" + e.message[0]["message"] + "</p>"

    folder_list = []

    # Get all of the folders
    folders = course.get_folders()

    for folder in folders:
        folder_item = {
            "id": folder.id,
            "name": folder.name,
            "parent_folder_id": folder.parent_folder_id,
            "files_url": folder.files_url,
            "files": [],
            "folders": [],
        }
        folder_list.append(folder_item)

    # Get all of the files for each folder
    files = course.get_files(
        content_types=[
            "image",
        ]
    )
    for folder in folder_list:
        for file in files:
            file_item = {
                "id": file.id,
                "name": file.display_name,
                "parent_folder_id": file.folder_id,
                "filename": file.filename,
            }
            if file.folder_id == folder["id"]:
                folder["files"].append(file_item)

    # Create an html file tree to place in the template
    final_output = create_tree(folder_list)

    return render_template(
        "select.htm.j2", launch_id=launch_id, final_output=final_output
    )


# Redirects back to Home
@app.route("/select/back/", methods=["POST"])
def selectBack():
    launch_id = session["launch_id"]

    return render_template("start.htm.j2", launch_id=launch_id)


# Route to view File Uploader
@app.route("/upload/", methods=["GET"])
def upload():
    launch_id = session["launch_id"]
    return render_template("upload.htm.j2", launch_id=launch_id)


@app.route("/uploader", methods=["POST"])
def uploader():
    if request.method == "POST":
        f = request.files["userfile"]
        session["filename"] = secure_filename(f.filename)
        if f:
            f.save(os.path.join("images", secure_filename(f.filename)))
            return redirect(
                url_for("load", filename=session["filename"], fileid="upload")
            )
        else:
            return redirect(url_for("upload"))


# Redirects back to Home
@app.route("/upload/back/", methods=["POST"])
def homeBack():
    launch_id = session["launch_id"]
    return render_template("start.htm.j2", launch_id=launch_id)


# Route to save filename and ext type to session
@app.route("/load/", methods=["GET"])
def load():
    course_id = session["course_id"]
    filename = request.args["filename"]
    file_id = request.args["fileid"]
    session["ext"] = os.path.splitext(filename)[1]
    session["filename"] = filename

    if not file_id == "upload":
        # Save the file contents to a temp folder (create the folder if it does not exist)
        if not os.path.exists("images"):
            os.makedirs("images")
        r = requests.get(
            "{}/api/v1/courses/{}/files/{}/".format(API_URL, course_id, file_id),
            headers={"Authorization": "Bearer " + API_KEY},
        )
        download_url = requests.get(r.json()["url"])

        file_path = os.path.join("images/" + filename)

        with open(file_path, "wb") as f:
            f.write(download_url.content)

    return redirect(url_for("embed"))


@app.route("/embed/", methods=["GET"])
def embed():
    # network configs
    tool_conf = get_lti_config(session["iss"], session["client_id"])
    flask_request = FlaskRequest()
    launch_data_storage = get_launch_data_storage()
    launch_id = session["launch_id"]
    message_launch = ExtendedFlaskMessageLaunch.from_cache(
        launch_id, flask_request, tool_conf, launch_data_storage=launch_data_storage
    )

    filename = session["filename"]
    data = pytesseract.image_to_string(Image.open("./images/" + filename))
    os.remove("./images/" + filename)

    # file_path = os.path.join("images/", session["filename"] + ".html")
    # print(file_path)
    # with open(file_path, "r") as file:
    #     data = file.read()

    # Prepares the Embedding (Deep Link Resource) and returns the html onto the page
    resource = DeepLinkResource()
    resource.set_type("html").set_title("file").set_html(data)
    result = message_launch.get_deep_link().output_response_form([resource])

    return result
