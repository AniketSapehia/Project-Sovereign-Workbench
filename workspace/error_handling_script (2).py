def run_and_handle_error():
    # Create a dictionary with some sample data
    sample_dict = {'key1': 'value1', 'key2': 'value2'}

    # Attempt to access a non-existent key
    try:
        value = sample_dict['non_existent_key']
    except KeyError as e:
        print(f"Caught a KeyError: {e}")
        # Optionally, you can log the error or handle it in a specific way
        return f"Caught a KeyError: {e}"
    else:
        print("Operation successful, no KeyError occurred.")
        return value