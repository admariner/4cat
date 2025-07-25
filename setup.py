from setuptools import setup
import os

with open("README.md", 'r') as readmefile:
	readme = readmefile.read()

with open("VERSION") as versionfile:
	version = versionfile.readline().strip()

# Universal packages
packages = set([
	"anytree~=2.8.0",
	"atproto>=0.0.58",
	"bcrypt~=3.2.0",
	"beautifulsoup4",
	"clarifai-grpc~=9.0",
	"cryptography>=39.0.1",
	"cssselect~=1.1.0",
	"datedelta~=1.4.0",
	"dateparser~=1.1.0",
	"emoji>=2.12.1",
	"flag",
	"Flask~=3.0",
	"Flask_Limiter[memcached]",
	"Flask_Login~=0.6",
	"gensim>=4.3.3, <4.4.0",
	"google_api_python_client",
	"html2text==2020.*",
	"ImageHash>4.2.0",
	"jieba",
	"json_stream",
	"jsonschema",
	"langchain_core",
	"langchain_community",
	"langchain_anthropic",
	"langchain_google_genai",
	"langchain_ollama",
	"langchain_openai",
	"langchain_mistralai",
	"lxml",
	"markdown2==2.4.2",
	"nltk~=3.9.1",
	"networkx~=2.8.0",
	"numpy>=1.19.2",
	"oslex",
	"packaging",
	"pandas",
	"Pillow>=10.3",
	"praw~=7.0",
	"prawcore~=2.0",
	"psutil~=5.0",
	"psycopg2~=2.9.0",
	"pyahocorasick~=1.4.0",
	"pydantic",
	"pymemcache",
	"PyMySQL~=1.0",
	"pytest",
	"pytest-dependency",
	"PyTumblr==0.1.0",
	"razdel~=0.5",
	"requests~=2.27",
	"requests_futures",
	"ruff",
	"scenedetect[opencv]",
	"scikit-learn",
	"scipy~=1.13",
	"shapely",
	"svgwrite~=1.4.0",
	"Telethon~=1.36.0",
	"ural~=1.3",
	"unidecode~=1.3",
	"Werkzeug",
	"wordcloud~=1.8",
	# The https://github.com/akamhy/videohash is not being maintained anymore; these are two patches
	"imagedominantcolor @ git+https://github.com/dale-wahl/imagedominantcolor.git@pillow10",
	"videohash @ git+https://github.com/dale-wahl/videohash@main",
	"vk_api",
	"yt-dlp"
])

# Check for extension packages
if os.path.isdir("config/extensions"):
	extension_packages = set()
	for root, dirs, files in os.walk("config/extensions"):
		for file in files:
			if file == "requirements.txt":
				with open(os.path.join(root, file)) as extension_requirements:
					for line in extension_requirements.readlines():
						extension_packages.add(line.strip())
	if extension_packages:
		print("Found extensions, installing additional packages: " + str(extension_packages))
		packages = packages.union(extension_packages)

# Some packages don't run on Windows
unix_packages = set([
	"python-daemon==2.3.2"
])

if os.name != "nt":
	packages = packages.union(unix_packages)

setup(
	name='fourcat',
	version=version,
	description=('4CAT: Capture and Analysis Tool is a comprehensive tool for '
				 'analysing discourse on online social platforms'),
	long_description=readme,
	author="Open Intelligence Lab / Digital Methods Initiative",
	author_email="4cat@oilab.eu",
	url="https://4cat.nl",
	packages=['backend', 'webtool', 'datasources'],
	python_requires='>=3.11',
	install_requires=list(packages),
)
