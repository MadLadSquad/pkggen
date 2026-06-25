#!/usr/bin/env python3
import os
import sys
import yaml
import lib

def build_copyright(gentoo):
    copyright_obj = gentoo.get('copyright')

    if copyright_obj:
        years = copyright_obj.get('years', '')
        owner = copyright_obj.get('owner', '')

        license = copyright_obj.get('license', '')

        with open('COPYRIGHT.txt', 'w') as f:
            f.write(f"Copyright {years} {owner}. Distributed under the terms of the {license}.\n")

def build_profiles(gentoo):
    os.makedirs('profiles', exist_ok=True)

    # Write custom categories if any
    categories = gentoo.get('categories')
    if categories:
        with open('profiles/categories', 'w') as f:
            for cat in categories:
                f.write(f"{cat}\n")

    # Standard Gentoo overlays need to have a repo_name file with the name of the repo
    with open('profiles/repo_name', 'w') as f:
        f.write(f"{gentoo["name"]}\n")

def build_metadata(gentoo):
    os.makedirs('metadata', exist_ok=True)

    def to_bool_str(val, default):
        if val is None:
            return default
        return str(val).lower()

    thin = to_bool_str(gentoo.get('thin-manifests'), 'true')
    sign = to_bool_str(gentoo.get('sign-manifests'), 'false')
    masters = gentoo.get('masters')
    if masters is None:
        masters = 'gentoo'

    layout_conf = f"""repo-name = {gentoo["name"]}
thin-manifests = {thin}
sign-manifests = {sign}
profile-formats = portage-2
cache-formats = md5-dics
masters = {masters}
"""
    with open('metadata/layout.conf', 'w') as f:
        f.write(layout_conf)

def build_repositories_xml(gentoo):
    repo_xml = ['<repositories encoding="unicode" version="1.1">']

    priority = gentoo.get('priority')
    quality = gentoo.get('quality')

    repo_tag = '  <repo'
    if priority is not None:
        repo_tag += f' priority="{priority}"'
    if quality is not None:
        repo_tag += f' quality="{quality}"'
    repo_tag += '>'
    repo_xml.append(repo_tag)

    repo_xml.append(f'    <name>{gentoo["name"]}</name>')

    description = gentoo.get('description')
    if description:
        repo_xml.append(f'    <description>{description}</description>')

    owners = gentoo.get('owners', [])
    for owner in owners:
        o_name = owner.get('name')
        o_email = owner.get('email')
        if o_name or o_email:
            repo_xml.append('    <owner>')
            if o_name:
                repo_xml.append(f'      <name>{o_name}</name>')
            if o_email:
                repo_xml.append(f'      <email>{o_email}</email>')
            repo_xml.append('    </owner>')

    source = gentoo.get('source')
    if source:
        s_type = source.get('type')
        s_url = source.get('url')
        if s_type and s_url:
            repo_xml.append(f'    <source type="{s_type}">{s_url}</source>')

    repo_xml.append('  </repo>')
    repo_xml.append('</repositories>')

    with open('repositories.xml', 'w') as f:
        f.write('\n'.join(repo_xml) + '\n')

def main():
    try:
        input_data = lib.readinput()
        if not input_data:
            return
        data = yaml.safe_load(input_data)
    except Exception as e:
        print(f"Error parsing YAML: {e}", file=sys.stderr)
        sys.exit(1)

    if not data or 'repositories' not in data or 'gentoo' not in data['repositories']:
        return

    gentoo = data['repositories']['gentoo']
    
    name = gentoo.get('name')
    if not name:
        print("Error: gentoo repository name is missing", file=sys.stderr)
        sys.exit(1)

    build_copyright(gentoo)
    build_profiles(gentoo)
    build_metadata(gentoo)
    build_repositories_xml(gentoo)

if __name__ == "__main__":
    main()
