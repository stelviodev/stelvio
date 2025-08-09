from pelican import Pelican
from pelican.settings import read_settings

from stelvio.app import StelvioApp
from stelvio.aws.dns import Route53Dns
from stelvio.aws.s3 import S3StaticWebsite
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("s3-static-pelican")
# app = StelvioApp("example-cf-2-0027")
CUSTOM_DOMAIN_NAME = "s3-static-pelican.r53.ectlnet.com"

dns = Route53Dns(zone_id="Z08488092RCBV4ZZV4EJ8")
# dns = CloudflareDns(zone_id="ec65067170190f8207c119856299d07d")


@app.config
def configuration(_: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(),
        dns=dns,
    )


@app.run
def run() -> None:
    settings = read_settings(path="pelicanconf.py")
    settings['PATH'] = 'content'
    pelican = Pelican(settings)
    pelican.run()

    _ = S3StaticWebsite(
        "s3-static-mkdocs",
        directory="output",
        custom_domain="s3-2." + CUSTOM_DOMAIN_NAME,
    )
