# Valuation of Fine Stringed Instruments Using Auction Market Data
This repository is for a data science project as part of the fulfillment of The Erdös Institute Data Science Bootcamp of Spring 2026.

Contributors: [Pierre-Yves Bienvenu](https://github.com/NaturfreundFRA), [Sean Hays](https://github.com/sphays), [Jutta Kurth](https://github.com/jgkurth), [Nicole Zalewski](https://github.com/nicole-zalewski)

Deliverables:

* [Executive Summary](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/Deliverables/Executive%20report%20-%20Valuation%20Stringed%20Instruments%20-%20filled%20and%20styled.docx.pdf)
* [Video Presentation](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/Deliverables/Presentation.mp4)

## Introduction

This project aims to build a machine learning regression model to predict the market prices of fine stringed instruments, including violins, violas, cellos, and bows. The goal is to estimate fair market values based on key attributes such as maker, year of construction, instrument type, origin, and provenance proxies.

## Source of data

Data was collected via web scraping from [Tarisio.com](https://tarisio.com/), a major auction house specializing in fine stringed instruments. The site maintains the Cozio Archive, which aggregates sales data from over 40 auction houses dating back to 1829.

## Files

### Data

* [Economic_Data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/Economic_Data): Contains economic market indicators useful for predicting market prices.

* [Geo_Data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/Geo_Data): Data used to group instrument makers by region.

* [Tarisio_data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/Tarisio_Data): Contains a collection of over 50,000 auction sales and profiles of instrument makers. These are the raw datasets, straight from scraping.

* [generated_data](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/tree/main/Data/train_valid_test): Includes the data with enhanced features and a stratified train-validation-test split of our dataset.


### Source

* [tarisio_scraper](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/source/tarisio_scraper.py): This script is used to scrape sales data from Tarisio.com. It returns a sales data set and a makers data set (and optionally an instrument data set, ultimately not used here).

* [Enhanced_Feature_Generation](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/source/Enhanced_Feature_Generation.py): Script that creates new, enhanced economic and geographic features.

* [DataSplitting.py](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/source/DataSplitting.py): This script performs a stratified train-validation-test split of our dataset and also fills missing city/region data based on the maker (using only information from the train set). 

### Notebooks

* [Exploratory_data_analysis](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/exploratory_data_analysis_advanced.ipynb): Code that gives key statistics, visualizations, and insights regarding the dataset.

* [Model_training_testing](https://github.com/Erdos-Projects/spring-2026-valuation-of-stringed-instruments-II/blob/main/Model_training_testing.ipynb): Code that performs feature selection, evaluation of models on validation set, hyperparameter tuning, test set evaluation, and error analysis.

