from setuptools import setup

setup(name='pyvisonic',
      version='0.0.1',
      description='Library to interface with Visonic alarm systems',
      download_url='https://github.com/wwolkers/pyvisonic',
      author='wwolkers',
      license='MIT',
      packages=['pyvisonic'],
      install_requires=[
      'asyncio',
      'pyserial==3.2.1',
      'pyserial-asyncio',
      'python-dateutil'
      ],
      zip_safe=True)