# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import print_function
import yaml
import json
import boto3
import traceback
from boto3.session import Session
import zipfile
import botocore
import uuid
import os
import datetime

code_pipeline = boto3.client('codepipeline')
sts_client = boto3.client('sts')
accountid = sts_client.get_caller_identity()["Account"]
client = boto3.client('servicecatalog')


def handler(event, context):
    """ Main method controlling the sequence of actions as follows
        1. Get JobID from input
        2. Get Job Data from input
        3. Get Artifact data from input
        4. Setup S3 Client
        5. Sync Codebase with Service Catalog
    :param event: Input json from code pipeline, containing job id and input artifacts
    :param context: Not used, but required for Lambda function
    :return: None
    :exception: Any exception
    """
    print(event)
    try:
        job_id = event['CodePipeline.job']['id']
        job_data = event['CodePipeline.job']['data']
        artifact_data = job_data['inputArtifacts'][0]
        s3 = setup_s3_client()
        sync_service_catalog(s3, artifact_data)
        put_job_success(job_id, "Success")


    except Exception as e:
        print('Function failed due to exception.')
        print(e)
        traceback.print_exc()
        put_job_failure(job_id, 'Function exception: ' + str(e))


def sync_service_catalog(s3, artifact):
    """ Pseudo logic as follows
        1. Extract S3 Zip file
        2. Iterate through all the folders starting with portfolio-
        3. Read mapping.yaml in each such folder. Refer Readme for more details on syntax
        4. If portfolio name matches, update the products by creating a new version
        5. If portfolio name does not matches, create a new one.
        6. Share the portfolio with list of accounts mentioned in the mapping.yaml
        7. Give access to the principals mentioned in the mapping.yaml
        8. Tag Portfolio as mentioned in mapping.yaml
    
    :param s3: S3 Boto3 client
    :param artifact: Artifact object sent by codepipeline
    :return: None
    """
    bucket = artifact['location']['s3Location']['bucketName']
    key = artifact['location']['s3Location']['objectKey']
    tmp_file = '/tmp/' + str(uuid.uuid4())
    s3.download_file(bucket, key, tmp_file)
    with zipfile.ZipFile(tmp_file, 'r') as zip:
        zip.extractall('/tmp/')
        print('Extract Complete')
    for folder in os.listdir('/tmp'):
        print('Checking object = ' + folder)
        if os.path.isdir("/tmp/" + folder):
            print('Found ' + folder + ' as folder')
            if str(folder).startswith('portfolio-'):
                print('Found ' + folder + ' as folder starting with portfolio')
                for mappingfile in os.listdir("/tmp/" + folder):
                    print('Found ' + mappingfile + ' inside folder ' + folder)
                    if str(mappingfile).endswith('mapping.yaml'):
                        print('Working with ' + mappingfile + ' inside folder ' + folder)
                        with open(("/tmp/" + str(folder) + "/" + str(mappingfile)), 'r') as stream:
                            objfile = yaml.load(stream)
                        # objfile = json.loads("/tmp/"+folder+"/"+mappingfile)
                        print('Loaded JSON=' + str(objfile))
                        lst_portfolio = list_portfolios()
                        lst_portfolio_name = []
                        obj_portfolio = {}
                        for portfolio in lst_portfolio:
                            if portfolio['DisplayName'] not in lst_portfolio_name:
                                lst_portfolio_name.append(portfolio['DisplayName'])
                        if objfile['name'] in lst_portfolio_name:
                            print('PORTFOLIO Match found.Checking Products now.')
                            for item in lst_portfolio:
                                if item['DisplayName'] == objfile['name']:
                                    portfolio_id = item['Id']
                                    obj_portfolio = item
                            update_portfolio(obj_portfolio, objfile, bucket)
                            remove_principal_with_portfolio(obj_portfolio['Id'])
                            associate_principal_with_portfolio(obj_portfolio, objfile)
                            lst_products = list_products_for_portfolio(portfolio_id)
                            lst_products_name = []
                            for products in lst_products:
                                lst_products_name.append(products['Name'])
                            for productsInFile in objfile['products']:
                                if productsInFile['name'] in lst_products_name:
                                    s3key = 'sc-templates/' + productsInFile['name'] + '/templates/' + str(
                                        uuid.uuid4()) + '.yaml'
                                    for ids in lst_products:
                                        if ids['Name'] == productsInFile['name']:
                                            productid = ids['ProductId']
                                    s3.upload_file(
                                        '/tmp/' + str(folder) + "/" + productsInFile['template'],
                                        bucket, s3key)
                                    create_provisioning_artifact(productsInFile, productid, bucket + "/" + s3key)
                                else:
                                    s3key = 'sc-templates/' + productsInFile['name'] + '/templates/' + str(
                                        uuid.uuid4()) + '.yaml'
                                    s3.upload_file(
                                        '/tmp/' + str(folder) + "/" + productsInFile['template'],
                                        bucket,
                                        s3key)
                                    create_product(productsInFile, portfolio_id, bucket + "/" + s3key)
                        else:
                            print('NO PORTFOLIO Match found.Creating one...')
                            create_portfolio_response = create_portfolio(objfile, bucket)
                            portfolioid = create_portfolio_response['PortfolioDetail']['Id']
                            associate_principal_with_portfolio(create_portfolio_response['PortfolioDetail'], objfile)
                            for productsInFile in objfile['products']:
                                s3key = 'sc-templates/' + productsInFile['name'] + '/templates/' + str(
                                    uuid.uuid4()) + '.yaml'
                                s3.upload_file(
                                    '/tmp/' + str(folder) + "/" + productsInFile['template'], bucket,
                                    s3key)
                                create_product(productsInFile, portfolioid, bucket + "/" + s3key)


def update_portfolio(objPortfolio, objMappingFile, bucket):
    """ Pseudo code as 
        1. Make describe_portfolio call
        2. Call update_portfolio to remove all tags and sync Description and ProviderName as mentioned in mapping file object
        3. Call update_portfolio to add the tags as mentioned in mapping file object
        4. Modify Bucket policy to sync access with the accountnumber
        5. Share portfolio with the accounts mentioned in the mapping file object
        6. Remove portfolio access from the accounts NOT mentioned in the mapping file object
    
    :param objPortfolio: Portfolio Object as retrieved from boto3 call
    :param objMappingFile: mapping.yaml file object
    :param bucket: S3 Bucket
    :return: 
    """
    describe_portfolio = client.describe_portfolio(Id=objPortfolio['Id'])
    objTags = []
    for tag in describe_portfolio['Tags']:
        if tag['Key'] not in objTags:
            objTags.append(tag['Key'])

    client.update_portfolio(
        Id=objPortfolio['Id'],
        Description=objMappingFile['description'],
        ProviderName=objMappingFile['owner'],
        RemoveTags=objTags
    )
    client.update_portfolio(
        Id=objPortfolio['Id'],
        AddTags=objMappingFile['tags']
    )
    bucket_policy = get_bucket_policy(bucket)
    policy = json.loads(bucket_policy['Policy'])
    statements = policy['Statement']
    objAccounts = []
    for account in objMappingFile['accounts']:
        if check_if_account_is_integer(account['number']) and str(account['number']) != accountid:
            objAccounts.append(str(account['number']))
    accounts_to_add = get_accounts_to_append(statements, objAccounts, bucket)
    if accounts_to_add:
        statements.append(create_policy(accounts_to_add, bucket))
    policy['Statement'] = statements
    put_bucket_policy(json.dumps(policy), bucket)
    share_portfolio(objAccounts, objPortfolio['Id'])
    remove_portfolio_share(objAccounts, objPortfolio['Id'])


def associate_principal_with_portfolio(objPortfolio, objMappingFile):
    """ Grants access to the Roles/Users as mentioned in the mappings object
    
    :param objPortfolio: Portfolio Object as retrieved from boto3 call
    :param objMappingFile: mapping.yaml file object
    :return: None
    """
    if len(objMappingFile['principals']) > 0:
        for principalarn in objMappingFile['principals']:
            client.associate_principal_with_portfolio(
                PortfolioId=objPortfolio['Id'],
                PrincipalARN="arn:aws:iam::"+accountid+":"+str(principalarn),
                PrincipalType='IAM'
            )



def remove_principal_with_portfolio(id):

    """ Removes access to the Roles/Users as mentioned in the mappings object

        :param objPortfolio: Portfolio Object as retrieved from boto3 call
        :param objMappingFile: mapping.yaml file object
        :return: None
    """

    list_principals = client.list_principals_for_portfolio(
        PortfolioId=id
    )
    for principal in list_principals['Principals']:
        client.disassociate_principal_from_portfolio(
            PortfolioId=id,
            PrincipalARN=principal['PrincipalARN']
        )


def list_portfolios():
    """ 
    :return:  List of Portfolios in the account
    """
    nextmarker = None
    done = False
    client = boto3.client('servicecatalog')
    lst_portfolio = []

    while not done:
        if nextmarker:
            portfolio_response = client.list_portfolios(nextmarker=nextmarker)
        else:
            portfolio_response = client.list_portfolios()

        for portfolio in portfolio_response['PortfolioDetails']:
            lst_portfolio.append(portfolio)

        if 'NextPageToken' in portfolio_response:
            nextmarker = portfolio_response['NextPageToken']
        else:
            break
    return lst_portfolio


def list_products_for_portfolio(id):
    """
    
    :param id: portfolio id
    :return: List of products associated with the portfolio
    """
    nextmarker = None
    done = False
    client = boto3.client('servicecatalog')
    lst_products = []

    while not done:
        if nextmarker:
            product_response = client.search_products_as_admin(nextmarker=nextmarker, PortfolioId=id)
        else:
            product_response = client.search_products_as_admin(PortfolioId=id)

        for product in product_response['ProductViewDetails']:
            lst_products.append(product['ProductViewSummary'])

        if 'NextPageToken' in product_response:
            nextmarker = product_response['NextPageToken']
        else:
            break
    return lst_products


def create_product(objProduct, portfolioid, s3objectkey):
    """
    
    :param objProduct: Product object to be created. has all the mandatory details for product creation
    :param portfolioid: Portfolio ID with which the newly created product would be associated with
    :param s3objectkey: S3Object Key, which has the cloudformation template for the product
    :return: None
    """
    client = boto3.client('servicecatalog')
    create_product_response = client.create_product(
        Name=objProduct['name'],
        Owner=objProduct['owner'],
        Description=objProduct['description'],
        SupportEmail=objProduct['owner'],
        ProductType='CLOUD_FORMATION_TEMPLATE',
        ProvisioningArtifactParameters={
            'Name': 'InitialCreation',
            'Description': 'InitialCreation',
            'Info': {
                'LoadTemplateFromURL': 'https://s3.amazonaws.com/' + s3objectkey
            },
            'Type': 'CLOUD_FORMATION_TEMPLATE'
        },
        IdempotencyToken=str(uuid.uuid4())
    )

    response = client.associate_product_with_portfolio(
        ProductId=create_product_response['ProductViewDetail']['ProductViewSummary']['ProductId'],
        PortfolioId=portfolioid
    )


def create_provisioning_artifact(objProduct, productid, s3objectkey):
    """
    
    :param objProduct: Product object for which the provisioning artifact (version of the product) will be created. has all the mandatory details for product.
    :param productid: Product ID
    :param s3objectkey: S3Object Key, which has the cloudformation template for the product
    :return: None
    """
    client = boto3.client('servicecatalog')
    response = client.create_provisioning_artifact(
        ProductId=productid,
        Parameters={
            'Name': str(uuid.uuid4()),
            'Description': str(datetime.datetime.now()),
            'Info': {
                'LoadTemplateFromURL': 'https://s3.amazonaws.com/' + s3objectkey
            },
            'Type': 'CLOUD_FORMATION_TEMPLATE'
        },
        IdempotencyToken=str(uuid.uuid4())
    )


def create_portfolio(objMappingFile, bucket):
    """
    
    :param objMappingFile: Object of the mapping file
    :param bucket: BucketName which holds the cloudformation templates for the products
    :return: Response of Create portfolio share API call
    """
    client = boto3.client('servicecatalog')
    response = client.create_portfolio(
        DisplayName=objMappingFile['name'],
        Description=objMappingFile['description'],
        ProviderName=objMappingFile['owner'],
        IdempotencyToken=str(uuid.uuid4()),
        Tags=objMappingFile['tags']
    )

    bucket_policy = get_bucket_policy(bucket)
    policy = json.loads(bucket_policy['Policy'])
    statements = policy['Statement']
    objAccounts = []
    for account in objMappingFile['accounts']:
        if check_if_account_is_integer(account['number']) and str(account['number']) != accountid:
            objAccounts.append(str(account['number']))
    accounts_to_add = get_accounts_to_append(statements, objAccounts, bucket)
    if accounts_to_add:
        statements.append(create_policy(accounts_to_add, bucket))
    policy['Statement'] = statements
    put_bucket_policy(json.dumps(policy), bucket)
    share_portfolio(objAccounts, response['PortfolioDetail']['Id'])
    remove_portfolio_share(objAccounts, response['PortfolioDetail']['Id'])
    return response


def remove_portfolio_share(lst_accounts, portfolioid):
    """ Removes the portfolio share
    :param lst_accounts: list of accounts to remove
    :param portfolioid: portfolio id from which to remove the share
    :return: None
    """
    lst_privledged_accounts = list_portfolio_shares(portfolioid)
    for account in lst_privledged_accounts:
        if account not in lst_accounts:
            client.delete_portfolio_share(
                PortfolioId=portfolioid,
                AccountId=account
            )
    if not lst_privledged_accounts:
        for account in lst_accounts:
            client.delete_portfolio_share(
                PortfolioId=portfolioid,
                AccountId=account
            )


def list_portfolio_shares(portfolioid):
    """ Lists the shares for the specified portfolio
    :param portfolioid: portfolio id to list the shares
    :return: List of accounts with which the portfolio is already shared with
    """
    nextmarker = None
    done = False
    lst_privledged_accounts = []
    client = boto3.client('servicecatalog')

    while not done:
        if nextmarker:
            lst_portfolio_access = client.list_portfolio_access(nextmarker=nextmarker, PortfolioId=portfolioid)
        else:
            lst_portfolio_access = client.list_portfolio_access(PortfolioId=portfolioid)

        for accounts in lst_portfolio_access['AccountIds']:
            lst_privledged_accounts.append(accounts)

        if 'NextPageToken' in lst_portfolio_access:
            nextmarker = lst_portfolio_access['NextPageToken']
        else:
            break
    print(lst_privledged_accounts)
    return lst_privledged_accounts


def share_portfolio(lst_accounts, portfolioid):
    """ Shares the portfolio with the specified account ids
    :param lst_accounts: list of accounts to share the portfolio with
    :param portfolioid: portfolio id
    :return: None
    """
    lst_privledged_accounts = list_portfolio_shares(portfolioid)
    if lst_accounts:
        for account in lst_accounts:
            if account not in lst_privledged_accounts:
                client.create_portfolio_share(
                    PortfolioId=portfolioid,
                    AccountId=str(account)
                )


def get_bucket_policy(s3bucket):
    """ Gets S3 Bucket policy
    :param s3bucket: S3 bucket to get the policy
    :return: Bucket Policy Object
    """
    s3_client = boto3.client('s3')
    try:
        bucket_policy = s3_client.get_bucket_policy(Bucket=s3bucket)
    except:
        bucket_policy = {u'Policy': u'{"Version":"2012-10-17","Id":"Default-Policy",'
                                    u'"Statement":[{"Sid":"' + str(uuid.uuid4()) +
                                    u'","Effect":"Allow",'
                                    u'"Principal":{"AWS":"arn:aws:iam::' + accountid +
                                    u':root"},"Action":"s3:GetObject",'
                                    u'"Resource":"arn:aws:s3:::' + s3bucket +
                                    u'/*"}]}'
                         }
    return bucket_policy


def create_policy(accountid, s3bucket):
    """ Creates the S3 Bucket policy and grants access to the specified accounts with which the portfolio is being shared
    :param accountid: AWS account id
    :param s3bucket: S3 Bucket
    :return: Built S3 Policy Object
    """
    policy = {
        "Sid": str(uuid.uuid1()),
        "Effect": "Allow",
        "Principal": {
            "AWS": accountid
        },
        "Action": "s3:GetObject",
        "Resource": [
            "arn:aws:s3:::" + s3bucket + "/sc-templates/*"
        ]
    }
    return policy


def get_accounts_to_append(statements, lstaccounts, s3bucket):
    """ Gets the accounts to append to the S3 Bucket policy
    :param statements: Existing statements
    :param lstaccounts: List of AWS accounts
    :param s3bucket: S3 Bucket
    :return: Object of principals to append in S3 policy
    """
    objPrincipals = []
    boolmatchfolder = False

    for statement in statements:
        if type(statement['Resource']) is unicode:
            if statement['Resource'] == "arn:aws:s3:::" + s3bucket + "/sc-templates/*":
                boolmatchfolder = True
        else:
            for folder in statement['Resource']:
                if folder == "arn:aws:s3:::" + s3bucket + "/sc-templates/*":
                    boolmatchfolder = True

    if boolmatchfolder:
        objexistingprincipals = []

        for statement in statements:
            if type(statement['Resource']) is unicode:
                if statement['Resource'] == "arn:aws:s3:::" + s3bucket + "/sc-templates/*":
                    if type(statement['Principal']['AWS']) is unicode:
                        if statement['Principal']['AWS'] not in objexistingprincipals:
                            objexistingprincipals.append(statement['Principal']['AWS'])
                    else:
                        for arn in statement['Principal']['AWS']:
                            if arn not in objexistingprincipals:
                                objexistingprincipals.append(arn)

            else:
                for folder in statement['Resource']:
                    if folder == "arn:aws:s3:::" + s3bucket + "/sc-templates/*":
                        for arn in statement['Principal']['AWS']:
                            if arn not in objexistingprincipals:
                                objexistingprincipals.append(arn)
        for account in lstaccounts:
            if ("arn:aws:iam::" + str(account) + ":root") not in objexistingprincipals:
                if "arn:aws:iam::" + str(account) + ":root" not in objPrincipals:
                    objPrincipals.append("arn:aws:iam::" + str(account) + ":root")
    else:
        for account in lstaccounts:
            if "arn:aws:iam::" + str(account) + ":root" not in objPrincipals:
                objPrincipals.append("arn:aws:iam::" + str(account) + ":root")

    return objPrincipals


def put_bucket_policy(policy, s3bucket):
    """ Puts the created S3 Bucket policy
    :param policy: Input Policy
    :param s3bucket: S3 Bucket
    :return: None
    """
    s3_client = boto3.client('s3')
    s3_client.put_bucket_policy(
        Bucket=s3bucket,
        Policy=policy
    )


def check_if_account_is_integer(string):
    """ Checks if the account number is an integer
    
    :param string: input string
    :return: Boolean, if the input is integer
    """

    try:
        int(string)
        return True
    except ValueError:
        return False


def setup_s3_client():
    """
    :return: Boto3 S3 session. Uses IAM credentials
    """
    session = Session()
    return session.client('s3', config=botocore.client.Config(signature_version='s3v4'))


def put_job_success(job, message):
    """Notify CodePipeline of a successful job

    Args:
        job: The CodePipeline job ID
        message: A message to be logged relating to the job status

    Raises:
        Exception: Any exception thrown by .put_job_success_result()

    """
    print('Putting job success')
    print(message)
    code_pipeline.put_job_success_result(jobId=job)


def put_job_failure(job, message):
    """Notify CodePipeline of a failed job

    Args:
        job: The CodePipeline job ID
        message: A message to be logged relating to the job status

    Raises:
        Exception: Any exception thrown by .put_job_failure_result()

    """
    print('Putting job failure')
    print(message)
    code_pipeline.put_job_failure_result(jobId=job, failureDetails={'message': message, 'type': 'JobFailed'})


def get_user_params(job_id, job_data):
    """ Gets User parameters from the input job id and data , sent from codepipeline
    :param job_id: Job ID
    :param job_data: Job data sent from codepipeline
    :return: Parameters sent from codepipeline
    :exception: Call put_job_failure to send failure to codepipeline
    """
    try:
        user_parameters = job_data['actionConfiguration']['configuration']['UserParameters']
        decoded_parameters = json.loads(user_parameters)
        print(decoded_parameters)
    except Exception as e:
        put_job_failure(job_id, e)
        raise Exception('UserParameters could not be decoded as JSON')

    return decoded_parameters


if __name__ == "__main__":
    handler(None, None)
