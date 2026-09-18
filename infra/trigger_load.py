import boto3
import concurrent.futures

LAMBDA_FUNCTION_NAME = "test-rca-app-test-conn-exhaust"
REGION = "us-east-1"
CONCURRENCY = 100

def invoke_lambda(i):
    # Initialize a new client per thread to ensure thread-safety
    client = boto3.client('lambda', region_name=REGION)
    try:
        response = client.invoke(
            FunctionName=LAMBDA_FUNCTION_NAME,
            InvocationType='RequestResponse'
        )
        status = response.get('StatusCode')
        
        # If the lambda threw an exception (e.g. out of DB connections)
        if 'FunctionError' in response:
            payload = response['Payload'].read().decode('utf-8')
            print(f"Request {i:02d} -> LAMBDA ERROR: {payload}")
        else:
            print(f"Request {i:02d} -> HTTP {status}")
            
    except Exception as e:
        print(f"Request {i:02d} -> BOTO3 ERROR: {str(e)}")

if __name__ == "__main__":
    print(f"Starting {CONCURRENCY} parallel Lambda invocations...")
    print("This will take about 10-15 seconds to run. Hold on tight!\n")
    
    # Fire off 60 concurrent requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [executor.submit(invoke_lambda, i) for i in range(1, CONCURRENCY + 1)]
        concurrent.futures.wait(futures)

    print("\nLoad test complete! Check your CloudWatch Alarms in the AWS Console.")
