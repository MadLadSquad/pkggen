#!/usr/bin/env python3
import os
import sys
import yaml
import lib

def main():
    folders = ['BUILD', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS']
    for folder in folders:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, '.gitkeep'), 'w') as f:
            pass

    gitignore_content = """*.tar.*
*.rpm
BUILD/*/
repodata/
"""
    with open('.gitignore', 'w') as f:
        f.write(gitignore_content)

if __name__ == "__main__":
    main()
