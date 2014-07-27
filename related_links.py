#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
import os
import sys
import argparse
from lxml import etree
import ConfigParser
from pprint import pprint as pp
import logging
from string import Template
import requests
import json
from time import sleep

NS = {'eac': 'urn:isbn:1-931666-33-4',
      'xlink': 'http://www.w3.org/1999/xlink'}

XPATH_HEADING = "/eac:eac-cpf/eac:cpfDescription/eac:identity/eac:nameEntry[1]" + \
    "/eac:part/text()"

XPATH_WIKIPEDIA = "//eac:cpfRelation[contains(@xlink:href,'en.wikipedia.org')]" + \
    "[@xlink:arcrole='http://socialarchive.iath.virginia.edu/control/term#sameAs']" + \
    "/@xlink:href"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='pre-process EAC for SNAC public access')
    parser.add_argument('data', nargs=1, help="directory with EAC XML files", )
    parser.add_argument('supplemental_data', nargs=1,
                        help="directory that holds output", )

    if argv is None:
        argv = parser.parse_args()

    sd_dir = argv.supplemental_data[0]

    assert os.path.isdir(sd_dir), \
        "supplemental_data {0} directory must exist".format(sd_dir)

    config = ConfigParser.ConfigParser()
    config.read('api.ini')

    print("starting")

    for root, ____, files in os.walk(argv.data[0]):
        for f in files:
            fullpath = os.path.join(root, f)
            supppath = os.path.join(sd_dir, f)
            if not os.path.isfile(supppath):
                process_file(fullpath, supppath, config)


def process_file(eac, newfile, config):
    xml = etree.parse(eac)
    name_heading = ' '.join(xml.xpath(XPATH_HEADING, namespaces=NS))
    wikipedia_url = xml.xpath(XPATH_WIKIPEDIA, namespaces=NS)

    assert name_heading, "must have a name"
    print(name_heading.encode('utf-8'))

    dpla_base = config.get('dpla', 'base')
    europeana_base = config.get('europeana', 'base')
    dbpedia_base = config.get('dbpedia', 'base')

    dpla_key = config.get('dpla', 'api_key')
    europeana_key = config.get('europeana', 'api_key')

    if wikipedia_url:
        wiki_thumb = wikipedia_sparql_query(wikipedia_url[0], dbpedia_base, 1)
    else:
        wiki_thumb = None

    dpla_link = dpla_query(name_heading, dpla_base, dpla_key, 1)
    europeana_link = False # forget eu

    et = etree.ElementTree(
        xml_template(
            name_heading, wiki_thumb, dpla_link, europeana_link
        )
    )
    et.write(newfile)


def xml_template(name_heading, wiki_thumb, dpla_link, europeana_link):
    supp = etree.Element('s', n=name_heading)
    if wiki_thumb and wiki_thumb['thumbnail']:
        supp.set('thumb', wiki_thumb['thumbnail'])
        supp.set('thumb_rights', wiki_thumb['attribution'])
    if dpla_link:
        supp.set('dpla', '1')
    if europeana_link:
        supp.set('europeana', '1')
    return supp


# "http://api.dp.la/v2/items?q=___&api_key=____&page_size=0"
def dpla_query(name_heading, base_url, api_key, polite_factor=1):
    params = {
        'q': name_heading,
        'api_key': api_key,
        'page_size': 0,
    }
    res = requests.get(url=base_url, params=params)
    try:
        res.raise_for_status()
    except requests.exceptions.HTTPError:
        return False
    sleeper(res, polite_factor)
    results = json.loads(res.text)
    if results['count'] > 0:
        return True
    else:
        return False


# "http://europeana.eu/api/v2/search.json?wskey=___&query=____&start=1&rows=0"
def europeana_query(name_heading, base_url, api_key, polite_factor=1):
    params = {
        'wskey': api_key,
        'query': name_heading.replace('/', ' ').replace(':', ' '),
        'start': 1,
        'rows': 0,
    }
    res = requests.get(url=base_url, params=params)
    try:
        res.raise_for_status()
    except requests.exceptions.HTTPError:
        return False
    sleeper(res, polite_factor)
    results = json.loads(res.text)
    if results['totalResults'] > 0:
        return True
    else:
        return False


def wikipedia_sparql_query(wikipedia_url, sparql_url, polite_factor=1):
    """lookup info from dbpedia"""
    # https://gist.github.com/tingletech/8643380
    dbpedia_url = wikipedia_url.replace(
        'http://en.wikipedia.org/wiki/',
        'http://dbpedia.org/resource/')
    query = Template("""select * where {
?thumbnail dc:rights ?attribution . { SELECT ?thumbnail WHERE {
<$resource> <http://dbpedia.org/ontology/thumbnail> ?thumbnail
} } } LIMIT 1""")
    query = query.substitute(resource=dbpedia_url)
    logging.info(query)
    logging.info(sparql_url)
    params = {
        "query": query,
        "default-graph-uri": 'http://dbpedia.org',
        "format": 'application/sparql-results+json',
        "timeout": 5000,
    }
    logging.debug(params)
    res = requests.get(url=sparql_url, params=params)
    res.raise_for_status()
    # added to support python version < 2.7,
    # otherwise timedelta has total_seconds()
    logging.info(res.text)
    results = json.loads(res.text)
    out = {}
    if len(results['results']['bindings']) > 0:
        attribution = results['results']['bindings'][0]['attribution']['value']
        thumbnail = results['results']['bindings'][0]['thumbnail']['value']
        thumbnail = thumbnail.replace('200px-', '150px-')
        out = {
            "attribution": attribution,
            "thumbnail": correct_url(thumbnail, attribution),
        }
    sleeper(res, polite_factor)
    return out


def sleeper(res, polite_factor):
    ms = res.elapsed.microseconds
    secs = res.elapsed.seconds
    days = res.elapsed.days
    seconds = (ms + (secs + days*24*3600) * 1e6) / 1e6
    sleep(seconds * polite_factor)
    logging.debug('waited for {0} seconds'.format(seconds * polite_factor))


def correct_url(url, rights):
    """
correct_url

link checker and guesser for wikipedia thunbnail URLs

returns a checked (good) URL as a unicode string or None
"""
    urlres = requests.head(url, allow_redirects=True)
    # thubmnail URL looks good (check the link first)
    if (urlres.status_code == requests.codes.ok):
        return url

    # something is not right
    # if the attribute page for the image does not exist, then we
    # won't find a thumbnail, so we may as well give up now
    rightsres = requests.head(rights)
    if (rightsres.status_code != requests.codes.ok):
        return None

    # okay, there should be a good thumbnail here, just not at the
    # URL we tried

    elif (urlres.status_code == 404):
        return correct_url_404(url)
    elif (urlres.status_code == 500):
        return correct_url_500(url)
    # not sure we can get here, something might be very wrong
    else:
        raise Exception("wikipedia thumbnail URL {0} had unexpected" +
                        "status code {1}".format(urlres.status_code, url))


def correct_url_404(url):
    # try english wikipedia
    url = url.replace('/commons/', '/en/', 1)
    res = requests.head(url)
    if (res.status_code == requests.codes.ok):
        return url
    elif (res.status_code == 500):
        return correct_url_500(url)
    # not sure we can get here, but don't panic if we do
    else:
        return None


def correct_url_500(url):
    # a 500 usually means the size we requested is too large
    for size in ['100', '75', '50', '25']:
        tryagain = try_smaller_image(url, size)
        if tryagain is not None:
            return tryagain
    # we gave it a shot, but that is one small image!
    return None


def try_smaller_image(url, size):
    string = u''.join(['/', size, 'px-'])
    url = url.replace('/150px-', string, 1)
    res = requests.head(url)
    if (res.status_code == requests.codes.ok):
        return url
    else:
        return None


# main() idiom for importing into REPL for debugging
if __name__ == "__main__":
    sys.exit(main())


# Copyright © 2014, Regents of the University of California
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# - Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# - Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
# - Neither the name of the University of California nor the names of its
# contributors may be used to endorse or promote products derived from this
# software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
