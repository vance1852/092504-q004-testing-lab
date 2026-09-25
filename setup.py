from setuptools import find_packages, setup

setup(
    name="skills-workspace",
    version="0.2.0",
    description="技能赛训协作基础服务与软件测试实验运行/缺陷复现平台",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
    entry_points={
        "console_scripts": [
            "skills-workspace-experiments=skills_workspace.cli:main",
        ],
    },
)
