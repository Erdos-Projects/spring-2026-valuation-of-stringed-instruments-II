# Valuation of Fine-Stringed Instruments Using Auction Market Data
This repository is for a data science project as part of the fulfillment of The Erdös Institute Data Science Bootcamp of Spring 2026.

Contributors: [Pierre-Yves Bienvenu](https://github.com/NaturfreundFRA), [Sean Hays](https://github.com/sphays), [Jutta Kurth](https://github.com/jgkurth), [Nicole Zalewski](https://github.com/nicole-zalewski)

Deliverables:

* Executive Summary
* Video Presentation

# Introduction

This project aims to build a machine learning regression model to predict the market prices of fine stringed instruments, including violins, violas, cellos, and bows. The goal is to estimate fair market values based on key attributes such as maker, year of construction, instrument type, origin, and provenance proxies.

# Dataset

Data is obtained through web scraping of [Tarisio.com](https://tarisio.com/), which is one of the major auction houses involving fine-stringed instruments. It maintains the Cozio archive, which aggregates sales from 40 different auction houses since 1829.

# Files

# Data

* [Economic_Data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/Economic_Data): Contains economic market indicators useful for predicting market prices

* [Geo_Data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/Geo_Data): Data used to group instrument makers by region

* [Tarisio_data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/Tarisio_Data): A collection of over 40,000 auction sales

# Notebooks and Scripts

* [Enhanced_Feature_Generation](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/source/Enhanced_Feature_Generation.py): Script that creates new, enhanced economic and geographic features.

* [Exploratory_data_analysis](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/exploratory_data_analysis_advanced.ipynb): Code that gives key statistics, visualizations, and insights regarding the dataset.

* [DataSplitting.py](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/source/DataSplitting.py): This script performs a stratified train-validation-test split of our dataset. It also fills in

* [Model_training_testing](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/Model_training_testing.ipynb): Code that performs feature selection, evaluation of models on validation set, hyperparameter tuning, test set evaluation, and error analysis.


This respository 

1. For exploratory data analysis (EDA) regard the two EDA notebooks.

2. Create new, enhanced economic and geographic features by running the respective script in the `source` folder as follows:

   `python Enhanced_Feature_Generation.py`

3.  Data Splitting and filling in missing city data based on maker can be found in `DataSplitting.py'

4. Feature selection, model training, testing and error analysis can be found in `Model_training_testing.ipynb`.
