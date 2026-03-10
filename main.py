import argparse

import maven_test_metrics
import extract_test_snippets
import run_maven_tests

from unittest.mock import patch

if __name__ == '__main__':
    # args = [
    #     '--projects', 'tests.txt',
    #     '--root', '/data/xuhaoran/github',
    #     '--output', 'test_metrics.csv',
    #     '--workers', '5',
    #     '--resume',
    #     '--append'
    # ]
    #
    # with patch('sys.argv', ['main.py'] + args):
    #     maven_test_metrics.main()

    # args = [
    #     '--csv', 'test_metrics.csv',
    #     '--root', '/data/xuhaoran/github',
    #     '--output', 'extracted_tests',
    #     '--mode', 'top',
    #     '--top-n', '100',
    #     '--sort-by', 'oracle_length'
    # ]
    #
    # with patch('sys.argv', ['main.py'] + args):
    #     extract_test_snippets.main()


    # Simplified args to use a list instead of a single string
    args = [
        '--projects', 'repos.txt',
        '--root', '/data/xuhaoran/github',
        '--output', './results',
        '--parallel', '8'
    ]

    with patch('sys.argv', ['main.py'] + args):
        run_maven_tests.main()