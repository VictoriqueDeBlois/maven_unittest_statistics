import argparse

from maven_test_metrics import main

if __name__ == '__main__':
    args = argparse.Namespace(
        projects='tests.txt',
        root='/data/xuhaoran/github',
        output='test_metrics.csv',
        workers=2,
    )
    main(args)