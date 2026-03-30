import maven_test_metrics
import extract_test_snippets
import run_maven_tests
import filter_integration_tests

from unittest.mock import patch

from dotenv import load_dotenv


def collect_all_tests():
    load_dotenv()
    args = [
        '--projects', 'all_repos.txt',
        '--root', '/data/xuhaoran/github',
        '--output', 'all_tests.csv'
    ]
    with patch('sys.argv', ['main.py'] + args):
        maven_test_metrics.main()

    args = [
        '--input', 'all_tests.csv',
        '--output', 'all_integration_code.csv',
    ]
    with patch('sys.argv', ['main.py'] + args):
        filter_integration_tests.main()

    args = [
        '--csv', 'all_integration_code.csv',
        '--root', '/data/xuhaoran/github',
        '--output', 'all_integration_tests',
        '--mode', 'all'
    ]
    with patch('sys.argv', ['main.py'] + args):
        extract_test_snippets.main()

if __name__ == '__main__':
    collect_all_tests()